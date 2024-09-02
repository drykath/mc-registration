from django import forms
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.forms.widgets import SelectDateWidget
from django.utils import timezone
from .models import (
    Convention, Registration, PaymentMethod, RegistrationUpgrade,
    RegistrationLevel, DealerRegistrationLevel, ShirtSize,
    CouponCode, CouponUse
)
from .widgets import BootstrapChoiceWidget
from datetime import date
import codecs
import os
import random
import re

BIRTH_YEAR_CHOICES = list(range(date.today().year, 1900, -1))

def validate_birthday(value):
    years = date.today().year - value.year

    try:
        birthdate = date(year=date.today().year, month=value.month, day=value.day)
    except ValueError as e:
        if value.month == 2 and value.day == 29:
            birthdate = date(year=date.today().year, month=2, day=28)
        else:
            raise e

    if date.today() < birthdate:
        years -= 1

    if years < 18:
        raise ValidationError("You must be 18 or older to register")

def build_countries():
    fp = codecs.open(os.path.join(os.path.dirname(__file__), 'countries.dat'), mode='r', encoding='utf-8')
    countries = fp.read().split(';')
    fp.close()
    # The Select widget expects a tuple of names and values.
    # For us, these are the same...
    return [(x,x) for x in countries]


class RegistrationLevelChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        if hasattr(obj, 'upgrade_registration_level'):
            return '{0} [${1:.02f}]'.format(obj.upgrade_registration_level.title, float(obj.price))
        else:
            return '{0} [${1:.02f}]'.format(obj.title, float(obj.price))


class CIEditForm(forms.ModelForm):
    class Meta:
        model = Registration
        fields = [
                  'first_name',
                  'last_name',
                  'badge_name',
                  'email',
                  'address',
                  'city',
                  'state',
                  'postal_code',
                  'country',
                  'registration_level',
                  'dealer_registration_level',
                  'birthday',
                  'shirt_size',
                  'emergency_contact',
                  'volunteer',
                  'volunteer_phone',
                  'notes',
                ]
        widgets = {
                   'birthday': SelectDateWidget(years=BIRTH_YEAR_CHOICES),
                   'country': forms.Select(choices=build_countries()),
                   'registration_level': BootstrapChoiceWidget(),
                   'dealer_registration_level': BootstrapChoiceWidget(),
                   'shirt_size': BootstrapChoiceWidget(),
                }

    registration_level = RegistrationLevelChoiceField(widget=BootstrapChoiceWidget(), empty_label=None, queryset=RegistrationLevel.objects.none())

    def clean_birthday(self):
        data = self.cleaned_data['birthday']
        validate_birthday(data)
        return data

    def clean_badge_name(self):
        data = self.cleaned_data['badge_name']
        # Ugh.  This is some RE magic.  space is \x20, and we want to allow all characters thru \x7e (~)
        # This will include alphanumerics and simple punctuation.
        if re.match('[^\x20-\x7e]', data):
            raise ValidationError("Badge name may only contain letters, numbers and punctuation.")

        return data

    def clean_registration_level(self):
        data = self.cleaned_data['registration_level']
        if ( (data.deadline and data.deadline <= timezone.now()) or
           data.active == False or
           (data.limit and len(Registration.objects.filter(registration_level=data)) >= data.limit)):
            raise ValidationError("That registration level is no longer available.")

        return data

    def clean_dealer_registration_level(self):
        data = self.cleaned_data['dealer_registration_level']
        if data and data.convention.dealer_limit and len(Registration.objects.filter(dealer_registration_level=data)) + data.number_tables > data.convention.dealer_limit:
            raise ValidationError("That dealer registration level is no longer available.")

        return data

    def clean_volunteer_phone(self):
        data = self.cleaned_data['volunteer_phone']
        if not data and self.cleaned_data['volunteer']:
            raise ValidationError("A contact phone number is required for volunteering.")

        return data

    def __init__(self, *args, **kwargs):
        super(CIEditForm, self).__init__(*args, **kwargs)
        current_convention = Convention.objects.current()
        self.fields['registration_level'].empty_label = None
        self.fields['registration_level'].queryset=RegistrationLevel.current.all().order_by('seq')

        self.fields['dealer_registration_level'].empty_label = 'None'
        self.fields['dealer_registration_level'].queryset=DealerRegistrationLevel.objects.filter(convention=current_convention).order_by('number_tables')

        self.fields['shirt_size'].empty_label = None
        self.fields['shirt_size'].queryset=ShirtSize.objects.order_by('seq')


class CIPaymentForm(forms.Form):
    """Take payment for a registration or upgrade.
        By default, RegistrationLevels are used. But
        RegistrationUpgrades can be duck typed into place."""
    registration_level = RegistrationLevelChoiceField(widget=BootstrapChoiceWidget(), empty_label=None, queryset=RegistrationLevel.objects.none())

    def __init__(self, *args, **kwargs):
        if 'registration_levels' in kwargs:
            registration_levels = kwargs['registration_levels']
            del kwargs['registration_levels']
        else:
            registration_levels = None
        super(CIPaymentForm, self).__init__(*args, **kwargs)
        current_convention = Convention.objects.current()
        self.fields['registration_level'].empty_label = None
        if registration_levels:
            self.fields['registration_level'].queryset=registration_levels
        else:
            self.fields['registration_level'].queryset=RegistrationLevel.current.all().order_by('seq')


class RegistrationForm(forms.ModelForm):
    class Meta:
        model = Registration
        fields = [
                  'first_name',
                  'last_name',
                  'badge_name',
                  'email',
                  'email_me',
                  'address',
                  'city',
                  'state',
                  'postal_code',
                  'country',
                  'registration_level',
                  'dealer_registration_level',
                  'birthday',
                  'shirt_size',
                  'emergency_contact',
                  'volunteer',
                  'volunteer_phone',
                ]
        widgets = {
            'birthday': SelectDateWidget(years=BIRTH_YEAR_CHOICES),
            'country': forms.Select(choices=build_countries()),
            'registration_level': BootstrapChoiceWidget(),
            'dealer_registration_level': BootstrapChoiceWidget(),
            'shirt_size': BootstrapChoiceWidget(),
        }

    payment_method = forms.ModelChoiceField(widget=BootstrapChoiceWidget(), empty_label=None, queryset=PaymentMethod.objects.filter(active=True).order_by('seq'))
    registration_level = RegistrationLevelChoiceField(widget=BootstrapChoiceWidget(), empty_label=None, queryset=RegistrationLevel.objects.none())
    coupon_code = forms.CharField(required=False)
    tos = forms.BooleanField(required=True, label='I agree to the Motor City Furry Convention <a href="/tos/" target="_blank">Code of Conduct</a>.')

    def clean_birthday(self):
        data = self.cleaned_data['birthday']
        validate_birthday(data)
        return data

    def clean_badge_name(self):
        data = self.cleaned_data['badge_name']
        # Ugh.  This is some RE magic.  space is \x20, and we want to allow all characters thru \x7e (~)
        # This will include alphanumerics and simple punctuation.
        if re.match('[^\x20-\x7e]', data):
            raise ValidationError("Badge name may only contain letters, numbers and punctuation.")

        return data

    def clean_registration_level(self):
        data = self.cleaned_data['registration_level']
        if ( (data.deadline and data.deadline <= timezone.now()) or
           data.active == False or
           (data.limit and len(Registration.objects.filter(registration_level=data)) >= data.limit)):
            raise ValidationError("That registration level is no longer available.")

        return data

    def clean_dealer_registration_level(self):
        data = self.cleaned_data['dealer_registration_level']
        if data and len(Registration.objects.filter(dealer_registration_level=data)) + data.number_tables > data.convention.dealer_limit:
            raise ValidationError("That dealer registration level is no longer available.")

    def clean_payment_method(self):
        data = self.cleaned_data['payment_method']
        if data.active == False:
            raise ValidationError("That payment method is no longer available.")

        return data

    def clean_volunteer_phone(self):
        data = self.cleaned_data['volunteer_phone']
        if not data and self.cleaned_data['volunteer']:
            raise ValidationError("A contact phone number is required for volunteering.")

        return data

    def clean_coupon_code(self):
        # TODO: Actually do we even have any request/session_id access here? Or other ways to identify the user?
        # TODO: If so, this needs lots of testing. If not, we should find somewhere else to do this.
        # Rate limit failures to prevent rapid scanning of coupon codes, especially via Telegram
        # XXX: Alternatively, maybe we don't allow coupon codes to be used through the Telegram app
        #failure_cache_key = 'registration_cc_failures_{}'.format(
        #    request.META.get('HTTP_X_FORWARDED_FOR',
        #    request.META.get('REMOTE_ADDR')))
        #failure_threshold = 5

        #hits = cache.get(failure_cache_key, 0)
        #if hits > failure_threshold:
        #    raise ValidationError("That coupon code is not valid.")

        data = self.cleaned_data['coupon_code']
        if data:
            try:
                code = CouponCode.objects.get(code=data, convention=Convention.objects.current())
            except ObjectDoesNotExist:
                code = None

            if not code:
                #cache.set(failure_cache_key, hits + 1, 1800)
                raise ValidationError("That coupon code is not valid.")

            if code.single_use and CouponUse.objects.filter(coupon=code):
                raise ValidationError("That coupon code has already been used.")

        return data

    def __init__(self, *args, **kwargs):
        super(RegistrationForm, self).__init__(*args, **kwargs)
        current_convention = Convention.objects.current()
        self.fields['registration_level'].empty_label = None
        self.fields['registration_level'].queryset=RegistrationLevel.current.all().order_by('seq')

        for level in self.fields['registration_level'].queryset:
            if level.limit and len(Registration.objects.filter(registration_level=level)) >= level.limit:
                self.fields['registration_level'].widget.disable_option(level.id, 'Sold Out')

        self.fields['dealer_registration_level'].empty_label = 'None'
        self.fields['dealer_registration_level'].queryset=DealerRegistrationLevel.objects.filter(convention=current_convention).order_by('number_tables')

        self.fields['shirt_size'].empty_label = None
        self.fields['shirt_size'].queryset=ShirtSize.objects.order_by('seq')


class UserRegUpdateForm(forms.Form):
    """For the confirmation page where users can update their own reg"""
    new_badge_name = forms.CharField(required=False, max_length=32,
            help_text='New badge name. Leave blank to keep the same.')

    def clean_new_badge_name(self):
        data = self.cleaned_data['new_badge_name']
        # Ugh.  This is some RE magic.  space is \x20, and we want to allow all characters thru \x7e (~)
        # This will include alphanumerics and simple punctuation.
        if re.match('[^\x20-\x7e]', data):
            raise ValidationError("Badge name may only contain letters, numbers and punctuation.")

        return data


class UpgradeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return '{0} [+ ${1:.02f}]'.format(obj.upgrade_registration_level.title, float(obj.price))

class UpgradeForm(forms.Form):
    """Simplified RegistrationForm for upgrades."""

    # Will populate upgrade_level on initialization
    upgrade = UpgradeChoiceField(widget=BootstrapChoiceWidget(), empty_label=None, queryset=RegistrationUpgrade.objects.none())
    payment_method = forms.ModelChoiceField(widget=BootstrapChoiceWidget(), empty_label=None, queryset=PaymentMethod.objects.filter(active=True, is_credit=True).order_by('seq'))
    coupon_code = forms.CharField(required=False)
    tos = forms.BooleanField(required=True, label='I agree to the Motor City Furry Convention <a href="/tos/" target="_blank">Code of Conduct &amp; Terms and Conditions</a>.')

    def clean_payment_method(self):
        data = self.cleaned_data['payment_method']
        if data.active == False:
            raise ValidationError("That payment method is no longer available.")

        return data

    def clean_upgrade(self):
        data = self.cleaned_data['upgrade']
        level = data.upgrade_registration_level
        if ( (level.deadline and level.deadline <= timezone.now()) or
                (level.limit and len(Registration.objects.filter(registration_level=level)) >= level.limit)):
            raise ValidationError("That registration level is no longer available.")

        return data

    def clean_coupon_code(self):
        data = self.cleaned_data['coupon_code']
        if data:
            if not self.allow_coupon_code:
                raise ValidationError("This registration has previously used a coupon code, a second code can not currently be used.")

            try:
                code = CouponCode.objects.get(code=data, convention=Convention.objects.current())
            except ObjectDoesNotExist:
                code = None

            if not code:
                raise ValidationError("That coupon code is not valid.")

            if code.single_use and CouponUse.objects.filter(coupon=code):
                raise ValidationError("That coupon code has already been used.")

        return data

    def __init__(self, *args, **kwargs):
        # New view style doesn't pass arguments, determine queryset here
        if 'initial' in kwargs and 'selected_registration' in kwargs['initial']:
            self.selected_registration = kwargs['initial']['selected_registration']
        else:
            # Otherwise, we'd expect it to be POSTed as 'registration'
            self.selected_registration = Registration.objects.get(id=args[0]['registration'])

        super(UpgradeForm, self).__init__(*args, **kwargs)

        if self.selected_registration:
            self.allow_coupon_code = self.selected_registration.couponuse_set.count() == 0
            # Calculate upgrade options given the selected registration
            upgrade_options = RegistrationUpgrade.objects.filter(active=True,
                                                                 current_registration_level=self.selected_registration.registration_level,
                                                                 #upgrade_registration_level__active=True,
            ).exclude(
                                                                 upgrade_registration_level__deadline__lt=timezone.now(),
            ).order_by('upgrade_registration_level__seq')
            self.fields['upgrade'].empty_label = None
            self.fields['upgrade'].queryset = upgrade_options

            for upgrade in self.fields['upgrade'].queryset:
                level = upgrade.upgrade_registration_level
                if level.limit and len(Registration.objects.filter(registration_level=level)) >= level.limit:
                    self.fields['upgrade'].widget.disable_option(upgrade.id, 'Sold Out')



class DealerUpgradeForm(forms.Form):
    """Simplified RegistrationForm for dealer upgrades."""

    # Will populate upgrade_level on initialization
    payment_method = forms.ModelChoiceField(widget=BootstrapChoiceWidget(), empty_label=None, queryset=PaymentMethod.objects.filter(active=True, is_credit=True).order_by('seq'))
    coupon_code = forms.CharField(required=True)
    tos = forms.BooleanField(required=True, label='I agree to the Motor City Furry Convention <a href="/tos/" target="_blank">Code of Conduct &amp; Terms and Conditions</a>.')

    def clean_payment_method(self):
        data = self.cleaned_data['payment_method']
        if data.active == False:
            raise ValidationError("That payment method is no longer available.")

        return data

    def clean_coupon_code(self):
        data = self.cleaned_data['coupon_code']
        if data:
            try:
                code = CouponCode.objects.get(code=data, convention=Convention.objects.current())
            except ObjectDoesNotExist:
                code = None

            if not code:
                raise ValidationError("That coupon code is not valid.")

            if not code.force_dealer_registration_level:
                raise ValidationError("This is not a dealer code.")

            if code.single_use and CouponUse.objects.filter(coupon=code):
                raise ValidationError("That coupon code has already been used.")

        return data

    def __init__(self, *args, **kwargs):
        # New view style doesn't pass arguments, determine queryset here
        if 'initial' in kwargs and 'selected_registration' in kwargs['initial']:
            self.selected_registration = kwargs['initial']['selected_registration']
        else:
            # Otherwise, we'd expect it to be POSTed as 'registration'
            self.selected_registration = Registration.objects.get(id=args[0]['registration'])

        super(DealerUpgradeForm, self).__init__(*args, **kwargs)
