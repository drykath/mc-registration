from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import View

from django.contrib import messages
from django.contrib.admin.models import LogEntry, CHANGE
from django.contrib.auth.decorators import user_passes_test
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.core.exceptions import PermissionDenied
from django.core.files import File
from django.core.mail import send_mail
from django.core.signing import TimestampSigner, BadSignature
from django.db import transaction
from django.db.models import Q
from django.template import loader
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.encoding import force_str
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

import json
import os
import qrcode
from datetime import timedelta
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

from .forms import CIEditForm, CIPaymentForm, RegistrationForm, UpgradeForm, DealerUpgradeForm, UserRegUpdateForm
from .models import (Convention, Registration, RegistrationLevel,
                     RegistrationUpgrade, DealerRegistrationLevel, RegistrationQueue,
                     Payment, PaymentMethod, CouponCode, CouponUse,
                     Swag, RegistrationSwag, ShirtSize,
                     RegistrationTempAvatar, BadgeAssignment, StaffRegistration
                     )
from .utils import PaymentError


# TODO: Preserve step1 to step2 outside of the session, probably relaying back through hidden fields or such, or serializing the data

class RegistrationDriver(View):
    '''
    Basic registration flow CBV, in a way that can be overridden for
    other related or similar processes.

    Overview of the expected steps and the functions defined here:
    1. Display form in form_class (step1_form)
       form_initial is used to customize form defaults.
       form_context is additional context data for templates.
    2. Let the user confirm what they've submitted and take payment (step2_confirm)
       If valid the submitted form is stored into the session.
       calculate_amount uses the submitted form and returns the amount.
       "amount" is injected into the context.
       Template's form needs only have "confirm".
    3. Show a success page (step3_success)
       process_payment should look for and process any payment information.
       success_save_form should do whatever the user came here to do.
       A confirmation email is sent to address in context variable "email".

    Subclasses can of course expand on those as needed.
    '''

    # Required variables for subclasses to set
    form_class = None
    form_template = None
    confirm_template = None
    success_template = None
    email_confirm_subject_template = None
    email_confirm_body_template = None

    # Methods for subclasses to override
    def form_initial(self, request, *args, **kwargs):
        '''Returns any initial values to give to the form'''

        return {}

    def form_context(self, request, *args, **kwargs):
        '''Returns additional context to pass to the form display'''

        return {
            'convention': self.current_convention,
        }

    def calculate_amount(self, form, *args, **kwargs):
        """
        Given the user's submitted form (either directly from POST data
        or cached) calculate the amount of this action. Subclasses must
        implement this.

        Expected to return a 3-tuple:
        0: The calculated amount
        1: Additional context variables to give to any viewed templates
        2: Description to give to the payment gateway
        """

        raise NotImplementedError

    def process_payment(self, request):
        """
        User has confirmed, process payment.

        Currently left as an exercise for the implementer, depending on
        your payment gateway and whatever code it needs to function.

        Return either a string representing whatever payment reference
        the gateway returns that should be preserved, None if you decide
        a payment isn't necessary, or raise PaymentError with the
        appropriate message if the gateway says the payment isn't
        successful.
        """

        raise NotImplementedError

    def success_save_form(self, request, form, amount, charge, **kwargs):
        '''
        Everything is successful, actually process the submitted form.
        Subclasses must implement this.

        Should return a dict with whatever newly created context is
        relevant to the subsequent templates, like the Reg object.
        '''

        raise NotImplementedError

    def ensure_ready(self, request, *args, **kwargs):
        # Ensure convention registration system is ready
        self.current_convention = Convention.objects.current()
        if not self.current_convention.registrationsettings.registration_open:
            raise PermissionDenied()

    def dispatch(self, request, *args, **kwargs):
        override = self.ensure_ready(request, *args, **kwargs)
        if override:
            return override
        return super(RegistrationDriver, self).dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        initial = self.form_initial(request, *args, **kwargs)
        form = self.form_class(initial=initial)
        return self.step1_form(request, form, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if 'confirm' in request.POST.keys():
            try:
                form = self.form_class(request.session['regformdata'])
            except KeyError:
                # If the form isn't serialized inside the session, the
                # user probably hit the back button after registering.
                return self.get(request, *args, **kwargs)
            if not form.is_valid():
                # Double check form validity
                return self.get(request, *args, **kwargs)
            return self.step3_success(request, form, *args, **kwargs)
        else:
            form = self.form_class(request.POST)
            if form.is_valid():
                return self.step2_confirm(request, form, *args, **kwargs)
            else:
                return self.step1_form(request, form, *args, **kwargs)

    def step1_form(self, request, form, *args, **kwargs):
        context = {
            'form': form,
        }
        context.update(self.form_context(request, *args, **kwargs))

        return render(request, self.form_template, context)

    def step2_confirm(self, request, form, *args, **kwargs):
        request.session['regformdata'] = form.cleaned_data

        amount, added_context, payment_description = self.calculate_amount(form, *args, **kwargs)

        context = {
            'form': form,
        }
        context.update(self.form_context(request, *args, **kwargs))
        context.update(added_context)

        context['amount'] = amount

        method = form.cleaned_data['payment_method']
        if amount == 0:
            method.is_credit = False
        context['method'] = method

        return render(request, self.confirm_template, context)

    def step3_success(self, request, form, *args, **kwargs):
        amount, added_context, payment_description = self.calculate_amount(form, *args, **kwargs)

        context = self.form_context(request, *args, **kwargs)
        context.update(added_context)

        try:
            charge = self.process_payment(request)
        except PaymentError as e:
            # Pass a "Payment Declined" error to the user
            form.add_error(None, e.args[0])
            request.session.pop('regformdata')
            return self.step1_form(request, form)
            # Process Stripe payment

        context.update(
            # success_save_form should return a dict, add that into context
            self.success_save_form(request, form, amount, charge, **context)
        )
        # Purge the old form from the session so it's no longer available
        request.session.pop('regformdata')

        # TODO: May be a better place to store the email address?
        self.send_confirmation_email(context['email'], context)
        return render(request, self.success_template, context)

    def send_confirmation_email(self, destination, context):
        email_subject = loader.render_to_string(
            self.email_confirm_subject_template, context
        )
        # Email subject *must not* contain newlines
        email_subject = ''.join(email_subject.splitlines())
        email_body = loader.render_to_string(
            self.email_confirm_body_template, context
        )
        send_mail(email_subject, email_body,
                  self.current_convention.contact_email,
                  [destination], fail_silently=True)


class Register(RegistrationDriver):
    """Initial registration process"""

    form_class = RegistrationForm
    form_template = 'registration/register.html'
    confirm_template = 'registration/confirm.html'
    success_template = 'registration/success.html'
    email_confirm_subject_template = 'registration/registration_confirm_subject.txt'
    email_confirm_body_template = 'registration/registration_confirm_body.txt'

    def form_initial(self, request):
        if request.user.is_authenticated:
            #initial = {'email': request.user.email, 'birthday': request.user.birth_day}
            initial = {'email': request.user.email}
        else:
            initial = {}

        # Allow registration levels to be pre-selected
        if 'as' in request.GET:
            candidates = RegistrationLevel.objects.filter(
                active=True, convention=self.current_convention,
                title__iexact=request.GET['as']
            )
            for level in candidates:
                initial['registration_level'] = str(level.id)
        initial['dealer_registration_level'] = str('')

        # TODO: Consider making this more generic by having initial inherit request.GET's dict
        if 'coupon_code' in request.GET:
            initial['coupon_code'] = request.GET['coupon_code']

        return initial

    def form_context(self, request):
        # Check for avatar upload
        if 'avatar' in request.FILES:
            # Hand off to upload handler
            handle_avatar_upload(request)

        if 'avatar' in request.session:
            avatar = RegistrationTempAvatar.objects.filter(id=request.session['avatar']).first()
        else:
            avatar = None

        if request.user.is_authenticated:
            # Look for any existing registrations the user may have
            registrations = [reg for reg in request.user.registration_set.filter(
                registration_level__convention=self.current_convention
            ) if reg.paid()]
        else:
            registrations = []

        return {
            'avatar': avatar,
            'convention': self.current_convention,
            'registrations': registrations,
        }

    def calculate_amount(self, form):
        '''
        Given the user's submitted form (either directly from POST data
        or cached) calculate the amount of this action.

        Returns a 3-tuple:
        0: The calculated amount
        1: Additional context variables to give to any viewed templates
        2: Description to give to the payment gateway
        '''

        reglevel = form.cleaned_data['registration_level']
        dealer_price = 0
        dealer_reglevel = None
        if form.cleaned_data['dealer_registration_level']:
            dealer_reglevel = form.cleaned_data['dealer_registration_level']
            dealer_price = dealer_reglevel.price
        discount_amount = 0
        discount_percent = 0
        code = None
        if form.cleaned_data['coupon_code']:
            code = CouponCode.objects.get(code=form.cleaned_data['coupon_code'])
            if code.percent:
                discount_percent = code.discount
            else:
                discount_amount = code.discount
            if code.force_registration_level:
                reglevel = code.force_registration_level
            if code.force_dealer_registration_level:
                dealer_reglevel = code.force_dealer_registration_level
                dealer_price = dealer_reglevel.price

        amount = max(((reglevel.price + dealer_price - discount_amount) * (1 - discount_percent)), 0)
        payment_description = '{dealer}{level}'.format(
            dealer='{title} Dealer '.format(title=dealer_reglevel.title) if dealer_reglevel else '',
            level=reglevel.title,
        )

        added_context = {
            'registration_level': reglevel,
            'dealer_registration_level': dealer_reglevel,
            'coupon_code': code,
            'email': form.cleaned_data['email'],
        }
        return amount, added_context, payment_description

    def success_save_form(self, request, form, amount, charge, **kwargs):
        '''Everything is successful, actually process the submitted form'''

        reg = form.save(commit=False)
        reg.ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR'))
        if request.user.is_authenticated:
            reg.user = request.user
        reg.registration_level = kwargs['registration_level']
        reg.dealer_registration_level = kwargs['dealer_registration_level']

        if charge or amount == 0:
            reg.status = 1
            reg.save()
            method = form.cleaned_data['payment_method']
            payment = Payment(registration=reg,
                              payment_method=method,
                              payment_amount=amount,
                              payment_level_comment=reg.registration_level.title,
                              payment_extra=charge.id if charge else None)
            payment.save()
        else:
            # Unpaid cash registration
            reg.status = 0
            reg.save()
            payment = None

        if kwargs['avatar']:
            del request.session['avatar']
            if 'avatar_original' in request.session:
                del request.session['avatar_original']
            reg.avatar = File(
                kwargs['avatar'].avatar,
                '{0}_{1}'.format(reg.id, os.path.basename(kwargs['avatar'].avatar.name))
            )
            reg.save()
            kwargs['avatar'].delete()

        if kwargs['coupon_code']:
            couponuse = CouponUse(registration=reg,
                                  coupon=kwargs['coupon_code'])
            couponuse.save()

        # If logged in clear navbar cache entry so that 'Not yet registered' disappears.
        if request.user.is_authenticated:
            cache.delete(make_template_fragment_key('site_navbar_userdata', [request.user.username]))

        return {
            'payment': payment,
            'registration': reg,
            'avatar': reg.avatar,
        }


class RegisterSimple(Register):
    '''Alternate registration process with a simpler form for on-site reg'''

    form_template = 'registration/register_simple.html'


class Upgrade(RegistrationDriver):
    '''Allow users to upgrade their registration'''

    form_class = UpgradeForm
    select_registration_template = 'registration/upgrade_select_registration.html'
    form_template = 'registration/upgrade.html'
    confirm_template = 'registration/upgrade_confirm.html'
    success_template = 'registration/upgrade_success.html'
    email_confirm_subject_template = 'registration/registration_upgrade_subject.txt'
    email_confirm_body_template = 'registration/registration_upgrade_body.txt'

    def ensure_ready(self, request, external_id=''):
        if not external_id and not request.user.is_authenticated:
            return HttpResponseRedirect('/accounts/login/?next={}'.format(request.path))
        return super(Upgrade, self).ensure_ready(request)

    def determine_registration(self, request, external_id=''):
        '''
        Tries to determine the registration to upgrade.
        If the ID is provided, look it up regardless of login status.
        Otherwise get user's registrations.

        Returns a 3-tuple:
        0: selected_registration, if able to determine
        1: Upgrade options for that registration, if relevant
        2: All of user's registrations, if relevant
        '''
        registrations = []
        # Kind of lame having to run this twice, in both initial and context
        if external_id:
            selected_registration = get_object_or_404(Registration, external_id=external_id, \
                                                      registration_level__convention=self.current_convention)
        else:
            # Look up active registrations for current user
            if request.user.is_authenticated:
                registrations = [reg for reg in request.user.registration_set.filter(
                    registration_level__convention=self.current_convention) if reg.paid()]
            selected_registration = None

            # Try to figure out which registration to upgrade ...
            if 'registration' in request.POST.keys() \
                    and request.POST['registration'].isdigit() \
                    and int(request.POST['registration']) in [reg.id for reg in registrations]:
                selected_registration = [reg for reg in registrations if reg.id == int(request.POST['registration'])][0]

            if len(registrations) == 1:
                selected_registration = registrations[0]

        if selected_registration:
            # Determine upgrade options and prices
            upgrade_options = RegistrationUpgrade.objects.filter(active=True,
                                                                 current_registration_level=selected_registration.registration_level,
                                                                 # upgrade_registration_level__active=True,
                                                                 ).exclude(
                upgrade_registration_level__deadline__lt=timezone.now(),
            ).order_by('upgrade_registration_level__seq')
        else:
            upgrade_options = []

        return selected_registration, upgrade_options, registrations

    def form_initial(self, request, external_id=''):
        selected_registration, upgrade_options, registrations = self.determine_registration(request, external_id)

        return {
            'selected_registration': selected_registration,
        }

    def form_context(self, request, external_id=''):
        selected_registration, upgrade_options, registrations = self.determine_registration(request, external_id)

        # ... Or have the user pick by swapping out the displayed template
        if not selected_registration or len(upgrade_options) == 0:
            self.form_template = self.select_registration_template

        return {
            'convention': self.current_convention,
            'registrations': registrations,
            'selected_registration': selected_registration,
            'external_id': external_id,
            'email': selected_registration.email if selected_registration else None,
        }

    def post(self, request, *args, **kwargs):
        # The user may be POSTing a registration selection, route back
        # to UpgradeForm to select upgrade path
        if 'upgrade' not in request.POST.keys():
            return self.get(request, *args, **kwargs)

        return super(Upgrade, self).post(request, *args, **kwargs)

    def calculate_amount(self, form, external_id=''):
        selected_upgrade = form.cleaned_data['upgrade']

        # Upgrade selected, figure out final amounts
        upgrade_price = selected_upgrade.price
        discount_amount = 0
        discount_percent = 0

        # Only accept a coupon if the registration hasn't already used one
        code = None
        if form.cleaned_data['coupon_code']:
            code = CouponCode.objects.get(code=form.cleaned_data['coupon_code'])
            if code.percent:
                discount_percent = code.discount
            else:
                discount_amount = code.discount
            # Force_registration_level must match for upgrades
            if code.force_registration_level \
                    and code.force_registration_level != selected_upgrade.id:
                code = None

        amount = max(((upgrade_price - discount_amount) * (1 - discount_percent)), 0)

        description = 'Upgrade {old} to {new}'.format(
            old=selected_upgrade.current_registration_level.title,
            new=selected_upgrade.upgrade_registration_level.title,
        )

        added_context = {
            'upgrade': selected_upgrade,
            'coupon': code,
        }
        return amount, added_context, description

    def success_save_form(self, request, form, amount, charge, **kwargs):
        """Everything is successful, actually process the submitted form"""

        selected_registration = kwargs['selected_registration']
        selected_upgrade = form.cleaned_data['upgrade']
        method = form.cleaned_data['payment_method']

        # Protect against anyone that submits a form without a payment
        # token or valid coupon code.
        if not charge and not kwargs['coupon']:
            raise PermissionDenied()

        selected_registration.registration_level = selected_upgrade.upgrade_registration_level
        # Mark the registration as needing re-printed
        if selected_registration.needs_print == 0:
            selected_registration.needs_print = 2
        selected_registration.save()

        payment = Payment(registration=selected_registration,
                          payment_method=method,
                          payment_amount=amount,
                          payment_level_comment=selected_upgrade.upgrade_registration_level.title + ' Upgrade',
                          payment_extra=charge.id if charge else None)
        payment.save()
        if kwargs['coupon']:
            couponuse = CouponUse(registration=selected_registration,
                                  coupon=kwargs['coupon'])
            couponuse.save()

        return {
            'payment': payment,
            'registration': selected_registration,
        }


class DealerUpgrade(Upgrade):
    """Allow users to add dealer tables their registration"""

    form_class = DealerUpgradeForm
    select_registration_template = 'registration/dealerupgrade_select_registration.html'
    form_template = 'registration/dealerupgrade.html'
    confirm_template = 'registration/dealerupgrade_confirm.html'
    success_template = 'registration/dealerupgrade_success.html'
    email_confirm_subject_template = 'registration/registration_dealerupgrade_subject.txt'
    email_confirm_body_template = 'registration/registration_dealerupgrade_body.txt'

    def ensure_ready(self, request, external_id=''):
        return super(Upgrade, self).ensure_ready(request)

    def form_context(self, request, external_id=''):
        selected_registration, upgrade_options, registrations = self.determine_registration(request, external_id)

        # ... Or have the user pick by swapping out the displayed template
        if not selected_registration:
            self.form_template = self.select_registration_template

        return {
            'convention': self.current_convention,
            'registrations': registrations,
            'selected_registration': selected_registration,
            'external_id': external_id,
            'email': selected_registration.email if selected_registration else None,
        }

    def post(self, request, *args, **kwargs):
        # If we get a confirmation code posted, redirect that back in
        if 'confirmation_code' in request.POST.keys() and request.POST['confirmation_code']:
            return redirect('dealer_upgrade', request.POST['confirmation_code'])

        # The user may be POSTing a registration selection, route back
        # to UpgradeForm to select upgrade path
        if 'coupon_code' not in request.POST.keys():
            return self.get(request, *args, **kwargs)

        return super(Upgrade, self).post(request, *args, **kwargs)

    def calculate_amount(self, form, external_id=''):
        discount_amount = 0
        discount_percent = 0

        # Dealer upgrades require a coupon code
        code = CouponCode.objects.get(code=form.cleaned_data['coupon_code'])
        dealer_reglevel = code.force_dealer_registration_level
        dealer_price = dealer_reglevel.price

        amount = max(((dealer_price - discount_amount) * (1 - discount_percent)), 0)

        description = 'Dealer level {title}'.format(
            title=dealer_reglevel.title,
        )

        added_context = {
            'dealer_registration_level': dealer_reglevel,
            'coupon': code,
        }
        return amount, added_context, description

    def success_save_form(self, request, form, amount, charge, **kwargs):
        '''Everything is successful, actually process the submitted form'''

        selected_registration = kwargs['selected_registration']
        code = CouponCode.objects.get(code=form.cleaned_data['coupon_code'])
        dealer_reglevel = code.force_dealer_registration_level
        method = form.cleaned_data['payment_method']

        if not charge:
            raise PermissionDenied()

        selected_registration.dealer_registration_level = dealer_reglevel
        selected_registration.save()

        payment = Payment(registration=selected_registration,
                          payment_method=method,
                          payment_amount=amount,
                          payment_level_comment='Dealer level {title}'.format(title=dealer_reglevel.title),
                          payment_extra=charge.id if charge else None)
        payment.save()
        couponuse = CouponUse(registration=selected_registration,
                              coupon=code)
        couponuse.save()

        return {
            'payment': payment,
            'registration': selected_registration,
        }


@require_POST
@csrf_exempt
def handle_avatar_upload(request):
    if 'avatar' in request.FILES:
        avatar = RegistrationTempAvatar(avatar=request.FILES['avatar'])
        avatar.save()
        request.session['avatar_original'] = avatar.id
    else:
        if 'avatar_original' in request.session:
            avatar = RegistrationTempAvatar.objects.filter(id=request.session['avatar_original']).first()
        else:
            raise PermissionDenied()

    if 'x' in request.POST.keys() and 'y' in request.POST.keys() \
            and request.POST['width'] != '0' and request.POST['height'] != '0':
        crop_data = request.POST
        # Validate data, just 500 on failure
        x = int(crop_data['x'])
        y = int(crop_data['y'])
        width = int(crop_data['width'])
        height = int(crop_data['height'])
        # Not used for now, but should still be present)
        rotate = int(crop_data['rotate'])
        scaleX = int(crop_data['scaleX'])
        scaleY = int(crop_data['scaleY'])

        # Crop the source image
        avatar_img = Image.open(avatar.avatar)
        avatar_img = avatar_img.crop((x, y, x + width, y + height))

        # Save to a new temp avatar
        im_output = BytesIO()
        avatar_img.convert('RGBA').save(im_output, format='png')
        im_output.seek(0)

        cropped_avatar = RegistrationTempAvatar()
        cropped_avatar.avatar.save(os.path.basename(avatar.avatar.name),
                                   File(im_output), save=True)

        request.session['avatar'] = cropped_avatar.id
    else:
        request.session['avatar'] = avatar.id

    data = {
        'avatar': request.session['avatar'],
        'full': request.session['avatar_original'],
        'height': avatar.avatar.height,
        'width': avatar.avatar.width,
    }
    return JsonResponse(data)


# TODO: Like check-in, change confirmation processes into a CBV
@transaction.atomic
def confirm(request, external_id):
    """Show confirmation for a registration, and allow for upgrades."""
    current_convention = Convention.objects.current()

    # Rate limit failures to this page to prevent scans for confirmation ID's
    # TODO: It'd kind of suck if the hotel's IP address got flagged here during the convention
    # Maybe add a whitelist above this, maybe only added to in the situation where it's been tripped.
    # And maybe base it on session then. I'm sure it'd be handy to have the hotel's IP elsewhere anyway.
    failure_cache_key = 'page_failures_{}'.format(
        request.META.get('HTTP_X_FORWARDED_FOR',
                         request.META.get('REMOTE_ADDR')))
    failure_threshold = 5

    hits = cache.get(failure_cache_key, 0)
    if hits > failure_threshold:
        raise Http404("No Registration matches the given query.")

    # Look up by external_id, but also force to current_convention
    try:
        reg = Registration.objects.get(external_id=external_id,
                                       registration_level__convention=current_convention)
    except Registration.DoesNotExist:
        cache.set(failure_cache_key, hits + 1, 1800)
        raise Http404("No Registration matches the given query.")

    # Determine upgrade availability
    upgrade_options = RegistrationUpgrade.objects.filter(active=True,
                                                         current_registration_level=reg.registration_level,
                                                         # upgrade_registration_level__active=True,
                                                         ).exclude(
        upgrade_registration_level__deadline__lt=timezone.now(),
    ).order_by('upgrade_registration_level__seq')

    upgrade_available = True
    if len(upgrade_options) == 0 or not reg.status == 1:
        upgrade_available = False

    return render(request, 'registration/user_confirmation.html', {'registration': reg,
                                                               'convention': current_convention,
                                                               'upgrade_available': upgrade_available})

@transaction.atomic
def confirm_change(request, external_id, confirmation=None):
    """Show confirmation for a registration, and allow for upgrades."""
    current_convention = Convention.objects.current()
    confirmation_hours = 24

    # Rate limit failures to this page to prevent scans for confirmation ID's
    # TODO: It'd kind of suck if the hotel's IP address got flagged here during the convention
    # Maybe add a whitelist above this, maybe only added to in the situation where it's been tripped.
    # And maybe base it on session then. I'm sure it'd be handy to have the hotel's IP elsewhere anyway.
    failure_cache_key = 'page_failures_{}'.format(
        request.META.get('HTTP_X_FORWARDED_FOR',
        request.META.get('REMOTE_ADDR')))
    failure_threshold = 5

    hits = cache.get(failure_cache_key, 0)
    if hits > failure_threshold:
        raise Http404("No Registration matches the given query.")

    # Look up by external_id, but also force to current_convention
    try:
        reg = Registration.objects.get(external_id=external_id,
            registration_level__convention=current_convention)
    except Registration.DoesNotExist:
        cache.set(failure_cache_key, hits + 1, 1800)
        raise Http404("No Registration matches the given query.")

    # Apply the same updateability logic as the confirmation page
    if reg.needs_print != 1 or reg.checked_in or reg.private_check_in:
        return redirect('convention_confirm', external_id)

    signer = TimestampSigner(salt='UserRegChange:{}'.format(str(reg.id)))
    if confirmation:
        try:
            # Attempt to read time-gated value
            replacement_id = signer.unsign(confirmation, max_age=timedelta(hours=confirmation_hours))
        except BadSignature:
            messages.warning(request, 'Could not update badge.')
            return redirect('convention_confirm', external_id)

        try:
            replacement = RegistrationTempAvatar.objects.get(id=replacement_id)
        except RegistrationTempAvatar.DoesNotExist:
            messages.warning(request, 'Could not update badge.')
            return redirect('convention_confirm', external_id)

        updated_info = 'User self-updated registration:'
        # Update requested data
        if replacement.new_badge_name:
            updated_info += ' Badge name (was "{}").'.format(reg.badge_name)
            reg.badge_name = replacement.new_badge_name
        if replacement.avatar:
            # TODO: Find a better way to not leave files dangling out there?
            updated_info += ' New avatar image (was "{}").'.format(reg.avatar)
            reg.avatar = File(
                replacement.avatar,
                '{0}_{1}'.format(reg.id, os.path.basename(replacement.avatar.name))
            )
        updated_info += ' On {}'.format(timezone.now())
        # Notes may be NULL, append gracefully
        note_update = reg.notes or ''
        # If there's any notes already, give a little space
        if note_update:
            note_update += '\n\n'
        note_update += updated_info
        reg.notes = note_update
        reg.save()
        replacement.delete()
        # Log it in the admin as well
        if request.user.is_authenticated:
            LogEntry.objects.log_action(
                user_id         = request.user.pk,
                content_type_id = ContentType.objects.get_for_model(reg).pk,
                object_id       = reg.pk,
                object_repr     = force_str(reg),
                action_flag     = CHANGE,
                change_message  = updated_info
            )
        messages.info(request, 'Your registration has been updated.')
        # TODO: Send a follow-up confirmation email, too?

        return redirect('convention_confirm', external_id)

    if 'new_badge_name' in request.POST.keys():
        update_form = UserRegUpdateForm(request.POST)
        if not update_form.is_valid():
            return redirect('convention_confirm', external_id)

        new_badge_name = update_form.cleaned_data['new_badge_name']
        # Check for avatar upload
        if 'avatar' in request.FILES:
            # Hand off to upload handler
            handle_avatar_upload(request)

        # Clean up
        if 'avatar_original' in request.session:
            del request.session['avatar_original']

        # Either way it may have been uploaded, check for an existing object
        if 'avatar' in request.session:
            regtemp = RegistrationTempAvatar.objects.filter(id=request.session['avatar']).first()
            if new_badge_name and new_badge_name != reg.badge_name:
                regtemp.new_badge_name = new_badge_name
                regtemp.save()
            del request.session['avatar']
        else:
            if new_badge_name and new_badge_name != reg.badge_name:
                regtemp = RegistrationTempAvatar.objects.create(new_badge_name=new_badge_name)
            else:
                # If they've changed neither, just don't do anything
                messages.info(request, 'No changes have been made to your registsration.')
                return redirect('convention_confirm', external_id)

        # Put together the confirmation materials: page and email
        confirmation = signer.sign(regtemp.id)
        confirm_context = {'registration': reg,
                           'convention': current_convention,
                           'newregistration': regtemp,
                           'hours': confirmation_hours,
                           'confirmation': confirmation,
                          }
        # Send the confirmation request email
        email_subject = loader.render_to_string(
            'registration/register_selfupdate_email_subject.txt', confirm_context
        )
        # Email subject *must not* contain newlines
        email_subject = ''.join(email_subject.splitlines())
        email_body = loader.render_to_string(
            'registration/register_selfupdate_email_body.txt', confirm_context
        )
        send_mail(email_subject, email_body,
                  current_convention.contact_email,
                  [reg.email], fail_silently=True)
        return render(request, 'registration/register_selfupdate_confirmation.html', confirm_context)
    else:
        update_form = UserRegUpdateForm()
        return render(request, 'registration/register_selfupdate.html', {'registration': reg,
                                                                 'convention': current_convention,
                                                                 'form': update_form,
                                                                })

    return redirect('convention_confirm', external_id)

@transaction.atomic
def confirm_claim(request, external_id):
    """Allow a reg without a user association to be claimed by a user"""
    current_convention = Convention.objects.current()

    # Rate limit failures to this page to prevent scans for confirmation ID's
    # TODO: It'd kind of suck if the hotel's IP address got flagged here during the convention
    # Maybe add a whitelist above this, maybe only added to in the situation where it's been tripped.
    # And maybe base it on session then. I'm sure it'd be handy to have the hotel's IP elsewhere anyway.
    failure_cache_key = 'page_failures_{}'.format(
        request.META.get('HTTP_X_FORWARDED_FOR',
        request.META.get('REMOTE_ADDR')))
    failure_threshold = 5

    hits = cache.get(failure_cache_key, 0)
    if hits > failure_threshold:
        raise Http404("No Registration matches the given query.")

    # Look up by external_id, but also force to current_convention
    try:
        reg = Registration.objects.get(external_id=external_id,
            registration_level__convention=current_convention)
    except Registration.DoesNotExist:
        cache.set(failure_cache_key, hits + 1, 1800)
        raise Http404("No Registration matches the given query.")

    if not reg.user and request.user.is_authenticated:
        # Update user association
        reg.user = request.user
        reg.save()
        # Log it
        LogEntry.objects.log_action(
            user_id         = request.user.pk,
            content_type_id = ContentType.objects.get_for_model(reg).pk,
            object_id       = reg.pk,
            object_repr     = force_str(reg),
            action_flag     = CHANGE,
            change_message  = 'User claimed registration into the account'
        )
        messages.info(request, 'Your registration has been updated to be associated with this account.')
        # Clear navbar cache fragment to remove the "You're not registered" message
        cache.delete(make_template_fragment_key('site_navbar_userdata', [request.user.username]))

    # Quietly redirect, even if we haven't done anything
    return redirect('convention_confirm', external_id)


# TODO: Consider refactoring, this totes may make more sense as a CBV.
#       It's only going to grow in complexity.
@user_passes_test(lambda u: (u.is_staff and u.is_superuser) or
                            (u.is_staff and u.groups.filter(name='registration').exists()) or
                            (u.is_staff and u.groups.filter(name__in=['reglead', 'constore', 'ops']).exists()) or
                            (u.groups.filter(name='regraf').exists()))
def check_in(request, registration_id=None, mode=None):
    """On-site registration check in
       registration_id: by number, if not provided, do a search
       mode: used on registration_id, if not provided, check in
        'edit': Allow edit of registration details
        'print': Pop-up badge print template
        'upgrade': Set new registration level and apply payment"""

    # Additional group-based security checks
    reg_lead = False
    if request.user.groups.filter(name='reglead').exists():
        # A lead logging in authorizes this system as a terminal that
        # rank-and-file registration staffers can use
        # See search.html below where the cookie is set
        reg_lead = True
    elif request.user.groups.filter(name='regraf').exists():
        # Only allow check-in for rank-and-file reg members to be done
        # on an authorized terminal.
        # Note above that these staffers don't need is_staff set, which
        # additionally keeps them entirely out of the admin.
        if not request.get_signed_cookie('terminal-auth', default=False, salt='terminal-auth'):
            return redirect('home')
    # Other future groups (such as 'constore' and 'ops') could have
    # additional permissions defined here later on. The 'registration'
    # group is also kept for the moment for backward compatibility, but
    # it's expected that may disappear in favor of the lead/raf split.

    # Only process for the currently active convention
    current_convention = Convention.objects.current()

    queue_name = 'regline'
    queued_registrations = []

    # Fetch the top 5 items in the queue
    registration_queue = RegistrationQueue.objects.filter(queue_name=queue_name)[:5]

    # 'touch' each queue item, and add to the list
    for queue_item in registration_queue:
        queue_item.check_visible()
        queued_registrations.append(queue_item.registration)

    # Settings
    regci_settings = {
            'regci_auto_request': request.session.get('regci_auto_request', False),
            'regci_swag_stats': request.session.get('regci_swag_stats', False),
            }

    # If con store wants estimates
    swag_stats = []
    if regci_settings['regci_swag_stats']:
        for swag in Swag.objects.filter(convention=current_convention):
            if swag.sizes:
                for size in ShirtSize.objects.all():
                    needed = 0
                    for level in swag.registrationlevel_set.all():
                        needed = needed + level.registration_set.filter(shirt_size=size).count()
                    swag_stats.append( {
                            'description': swag.description + ' (' + size.size + ')',
                            'needed': needed,
                            'received': swag.registrationswag_set.filter(size=size, received=True).count(),
                            'percent': (swag.registrationswag_set.filter(size=size, received=True).count() / (needed if needed > 0 else 1)) * 100,
                            } )
            else:
                needed = 0
                for level in swag.registrationlevel_set.all():
                    needed = needed + level.registration_set.count()
                swag_stats.append( {
                        'description': swag.description,
                        'needed': needed,
                        'received': swag.registrationswag_set.filter(received=True).count(),
                        'percent': (swag.registrationswag_set.filter(received=True).count() / (needed if needed > 0 else 1)) * 100,
                        } )

    if not registration_id:
        # TODO: Gotta be a better way of doing this
        # Maybe take care of it when/if this is restructured into a CBV
        # Toggle settings buttons
        if mode == 'regciautorequest':
            request.session['regci_auto_request'] = not regci_settings['regci_auto_request']
            return redirect('convention_check_in')
        if mode == 'regciswagstats':
            request.session['regci_swag_stats'] = not regci_settings['regci_swag_stats']
            return redirect('convention_check_in')

        # Find search parameters and break apart into sections
        registrations = []
        search = ""
        # Clear any previous swipe storage
        if 'c_last' in request.session:
            del request.session['c_last']
        if 'c_first' in request.session:
            del request.session['c_first']
        if 'c_birthday' in request.session:
            del request.session['c_birthday']

        if 'search' in request.POST.keys():
            search = request.POST['search']
            parameters = search.split()

            # Only search registrations for current convention
            registrations = Registration.all_registrations.filter(
                registration_level__convention=current_convention).order_by('id')

            # Each parameter, apply as an additional filter
            for parameter in parameters:
                registrations = registrations.filter(
                    Q(first_name__istartswith=parameter) | \
                    Q(last_name__istartswith=parameter) | \
                    Q(badge_name__istartswith=parameter) | \
                    Q(email__icontains=parameter) | \
                    Q(external_id__iexact=parameter)
                )

            # Search by badge number, if given a number
            for parameter in parameters:
                try:
                    badge_number = int(parameter)
                    registrations = registrations | Registration.all_registrations.filter(
                        registration_level__convention=current_convention,
                        id=badge_number + current_convention.registrationsettings.badge_offset).order_by('id')
                except ValueError:
                    pass

            # Limit display to 20 registrations
            registrations = registrations[:20]

        # Receive parsed card swipe data, do an intelligent search
        if 'c_last' in request.POST.keys():
            c_last = request.POST['c_last']
            c_first = request.POST['c_first']
            c_birthday = parse_date(request.POST['c_birthday'])
            # Stash items into session for usage later
            request.session['c_last'] = c_last
            request.session['c_first'] = c_first
            request.session['c_birthday'] = c_birthday

            # Only search registrations for current convention
            registrations = Registration.all_registrations.filter(
                registration_level__convention=current_convention).order_by('id')

            # Progressive intelligent search
            # 1. First, Last, Birthday
            found_registrations = registrations.filter(last_name__iexact=c_last,
                                                       first_name__iexact=c_first,
                                                       birthday=c_birthday)
            if len(found_registrations) == 0:
                # 2. Shortened first name? Try F. Initial, Last, Birthday
                found_registrations = registrations.filter(last_name__iexact=c_last,
                                                           first_name__istartswith=c_first[0],
                                                           birthday=c_birthday)
            if len(found_registrations) == 0:
                # 3. Birthday wrong? Try First, Last only
                found_registrations = registrations.filter(last_name__iexact=c_last,
                                                           first_name__iexact=c_first)
            if len(found_registrations) == 0:
                # 4. Wrong birthday, shortened name... F. Initial, Last
                found_registrations = registrations.filter(last_name__iexact=c_last,
                                                           first_name__istartswith=c_first[0])
            if len(found_registrations) == 0:
                # 5. Last effort, got married? First, Birthdate only
                found_registrations = registrations.filter(first_name__iexact=c_first,
                                                           birthday=c_birthday)

            registrations = found_registrations

        # If the search results in one registration, just bring it up
        if len(registrations) == 1:
            messages.info(request, 'Search found single registration, showing...')
            return redirect('convention_check_in', registrations[0].id)
        response = render(request, 'registration/check_in/search.html', {'search': search,
                                                                     'registrations': registrations,
                                                                     'queued_registrations': queued_registrations,
                                                                     'queue': queue_name,
                                                                     'settings': regci_settings,
                                                                     'swag_stats': swag_stats,
                                                                     'did_search': request.method == 'POST',
                                                                     'reg_lead': reg_lead})
        if reg_lead and not request.get_signed_cookie('terminal-auth', default=False, salt='terminal-auth'):
            # A lead logging in authorizes this system as a terminal that
            # rank-and-file registration staffers can use
            response = redirect('convention_check_in')
            response.set_signed_cookie('terminal-auth', request.user.username,
                                       salt='terminal-auth', max_age=4 * 24 * 60 * 60, secure=True, httponly=True)
        return response

    else:  # We were given a registration ID to process
        registration = get_object_or_404(Registration.all_registrations, id=registration_id, \
                                         registration_level__convention=current_convention)

        # Attempt to de-queue from any queue the reg might be in
        RegistrationQueue.dequeue(registration, queue_name)

        # First step, no "mode", just straight check-in
        if not mode:
            # Check-in mode is usually just that, with a POST to define that
            if 'set_check_in' in request.POST.keys():
                if request.POST['set_check_in'] == "1":
                    # If this reg is flagged, only a lead is allowed to check it in
                    if registration.private_check_in and not reg_lead:
                        messages.warning(request, 'Unable to check in this registration')
                    else:
                        # Check in
                        registration.checked_in = True
                        registration.checked_in_on = timezone.now()
                        registration.save()
                        LogEntry.objects.log_action(
                            user_id=request.user.pk,
                            content_type_id=ContentType.objects.get_for_model(registration).pk,
                            object_id=registration.pk,
                            object_repr=force_str(registration),
                            action_flag=CHANGE,
                            change_message='Checked in'
                        )
                        messages.success(request, '{} checked in.'.format(registration.badge_name))
                if request.POST['set_check_in'] == "0":
                    # Undo checkin procedure
                    registration.checked_in = False
                    registration.save()
                    LogEntry.objects.log_action(
                        user_id=request.user.pk,
                        content_type_id=ContentType.objects.get_for_model(registration).pk,
                        object_id=registration.pk,
                        object_repr=force_str(registration),
                        action_flag=CHANGE,
                        change_message='Undo check-in'
                    )
                    messages.warning(request, 'Marked {} as not checked in.'.format(registration.badge_name))
            # Explicitly add this registration to a queue
            if 'queue_name' in request.POST.keys():
                if 'room_number' in request.POST.keys() and request.POST['room_number']:
                    # TODO: Check is integer, gracefully fail
                    room_number = int(request.POST['room_number'])
                    registration.room_number = room_number
                    registration.save()
                RegistrationQueue.enqueue(registration, request.POST['queue_name'])
                messages.success(request, '{} added to queue {}.'.format(registration.badge_name, request.POST['queue_name']))
            # Record whether the user received swag
            if 'set_received_swag' in request.POST.keys():
                # Process updates to any existing markers
                for regswag in registration.registrationswag_set.all():
                    if ('received_{}'.format(regswag.id) not in request.POST.keys() and \
                            'backordered_{}'.format(regswag.id) not in request.POST.keys()):
                        regswag.delete()
                    else:
                        regswag_changed = False
                        if regswag.received != ('received_{}'.format(regswag.id) in request.POST.keys()):
                            regswag.received = 'received_{}'.format(regswag.id) in request.POST.keys()
                            regswag_changed = True
                        if regswag.backordered != ('backordered_{}'.format(regswag.id) in request.POST.keys()):
                            regswag.backordered = 'backordered_{}'.format(regswag.id) in request.POST.keys()
                            regswag_changed = True
                        if ('backorder_comment_{}'.format(regswag.id) in request.POST.keys() and \
                                regswag.backorder_comment != request.POST['backorder_comment_{}'.format(regswag.id)]):
                            regswag.backorder_comment = request.POST.get('backorder_comment_{}'.format(regswag.id),
                                                                         None)
                            regswag_changed = True
                        if ('size_{}'.format(regswag.id) in request.POST.keys() and \
                                regswag.size_id != request.POST['size_{}'.format(regswag.id)]):
                            regswag.size = ShirtSize.objects.get(pk=request.POST['size_{}'.format(regswag.id)])
                            regswag_changed = True
                        if regswag_changed:
                            regswag.save()
                # Look for items their registration level earns
                for swag in registration.registration_level.registrationlevelswag_set.all():
                    if ('new_received_{}'.format(swag.swag_id) in request.POST.keys() or \
                            'new_backordered_{}'.format(swag.swag_id) in request.POST.keys()):
                        regswag = RegistrationSwag.objects.create(
                            swag=swag.swag,
                            registration=registration,
                            received='new_received_{}'.format(swag.swag_id) in request.POST.keys(),
                            size=ShirtSize.objects.get(pk=request.POST['new_size_{}'.format(swag.swag_id)]) \
                                if 'new_size_{}'.format(swag.swag_id) in request.POST.keys() else None,
                            backordered='new_backordered_{}'.format(swag.swag_id) in request.POST.keys(),
                            backorder_comment=request.POST['new_backorder_comment_{}'.format(swag.swag_id)] \
                                if 'new_backorder_comment_{}'.format(swag.swag_id) in request.POST.keys() else None,
                        )
                LogEntry.objects.log_action(
                    user_id=request.user.pk,
                    content_type_id=ContentType.objects.get_for_model(registration).pk,
                    object_id=registration.pk,
                    object_repr=force_str(registration),
                    action_flag=CHANGE,
                    change_message='Recorded swag received'
                )
                messages.success(request, '{}: swag updated.'.format(registration.badge_name))
            # But it could also take the payment for cash on site
            payment_form = CIPaymentForm(initial={'registration_level': registration.registration_level})
            if 'registration_level' in request.POST.keys():
                payment_form = CIPaymentForm(request.POST)
                if payment_form.is_valid():
                    reglevel = payment_form.cleaned_data['registration_level']
                    # Cash includes cards swiped on Square
                    method = PaymentMethod.objects.get(name='Cash')
                    payment = Payment(registration=registration,
                                      payment_method=method,
                                      payment_amount=reglevel.price,
                                      created_by=request.user)
                    payment.save()
                    # Then update the registration
                    registration.registration_level = reglevel
                    registration.status = 1
                    registration.save()
                    LogEntry.objects.log_action(
                        user_id=request.user.pk,
                        content_type_id=ContentType.objects.get_for_model(registration).pk,
                        object_id=registration.pk,
                        object_repr=force_str(registration),
                        action_flag=CHANGE,
                        change_message='Payment taken for on-site registration.'
                    )
                    messages.success(request,
                                     'Payment accepted. {} can now be checked in.'.format(registration.badge_name))
            context = {'reg': registration, 'form': payment_form, 'reg_lead': reg_lead}
            # If we have some recorded card swipe data, compare that to the registration
            if 'c_last' in request.session:
                context['attempt_name'] = True
                context['c_last'] = request.session['c_last']
                context['c_first'] = request.session['c_first']
                if registration.last_name.lower() == request.session['c_last'].lower() and \
                        registration.first_name.lower() == request.session['c_first'].lower():
                    context['name_match'] = True
            if 'c_birthday' in request.session:
                context['attempt_birthday'] = True
                context['c_birthday'] = request.session['c_birthday']
                if registration.birthday == request.session['c_birthday']:
                    context['birthday_match'] = True

            # Immediately request badge be found and made available
            if regci_settings['regci_auto_request'] and registration.checked_in == False and registration.needs_print == 0 and registration.paid():
                RegistrationQueue.enqueue(registration, 'readybadge')

            # Determine received and due swag for this registration
            last_payment = None
            for payment in registration.payment_set.all():
                if not last_payment or payment.payment_received > last_payment:
                    last_payment = payment.payment_received

            received_swag = {}
            owed_swag = []
            # What they've received, if anything
            for regswag in registration.registrationswag_set.all():
                received_swag[regswag.swag_id] = regswag
            # What their registration level earns, but they haven't received
            for swag in registration.registration_level.registrationlevelswag_set.all():
                if swag.swag_id not in received_swag:
                    if not swag.must_register_before or (last_payment and last_payment < swag.must_register_before):
                        owed_swag.append(swag.swag)
            context['received_swag'] = received_swag
            context['owed_swag'] = owed_swag
            context['shirtsizes'] = ShirtSize.objects.all()
            context['vax_cutoff'] = timezone.now() - timedelta(weeks=2)
            context['queued_registrations'] = queued_registrations
            context['queue'] = queue_name
            context['settings'] = regci_settings
            context['swag_stats'] = swag_stats

            return render(request, 'registration/check_in/check_in.html', context)

        if mode == 'edit':
            # Show a registration edit form
            if not 'badge_name' in request.POST.keys():
                form = CIEditForm(instance=registration)
                return render(request, 'registration/check_in/edit.html', {'reg': registration,
                                                                       'form': form,
                                                                       'settings': regci_settings,
                                                                       'swag_stats': swag_stats,
                                                                       'reg_lead': reg_lead})
            # And then save the updated registration
            else:
                form = CIEditForm(request.POST, instance=registration)
                if form.has_changed():
                    if not form.is_valid():
                        return render(request, 'registration/check_in/edit.html', {'reg': registration,
                                                                               'form': form,
                                                                               'settings': regci_settings,
                                                                               'swag_stats': swag_stats,
                                                                               'reg_lead': reg_lead})
                    changed_fields = form.changed_data
                    if changed_fields:
                        change_message = 'Changed {0}.'.format(', '.join(changed_fields))
                        messages.info(request, 'Registration for {} has been changed.'.format(registration.badge_name))
                    else:
                        change_message = 'Saved with no changes'
                        messages.warning(request, 'No changes saved.')
                    form.save()
                    # Log the change into the admin's model history
                    LogEntry.objects.log_action(
                        user_id=request.user.pk,
                        content_type_id=ContentType.objects.get_for_model(registration).pk,
                        object_id=registration.pk,
                        object_repr=force_str(registration),
                        action_flag=CHANGE,
                        change_message='Changed {0}.'.format(', '.join(changed_fields))
                    )
                return redirect('convention_check_in', registration.id)

        if mode == 'print':
            # Show the printable badge in a pop-up window
            if not registration.status == 1:
                raise PermissionDenied()
            else:
                badge = BadgeAssignment(registration=registration, printed_by=request.user,
                                        registration_level=registration.registration_level)
                badge.save()
                # Mark the badge as not needing printed any longer
                registration.needs_print = 0
                registration.save()
                LogEntry.objects.log_action(
                    user_id=request.user.pk,
                    content_type_id=ContentType.objects.get_for_model(registration).pk,
                    object_id=registration.pk,
                    object_repr=force_str(registration),
                    action_flag=CHANGE,
                    change_message='Badge printed.'
                )
                return render(request, 'registration/badge.html', {'badges': [registration]})

        if mode == 'upgrade':
            payment_form = CIPaymentForm(registration_levels=registration.registration_level.upgrades)
            if 'registration_level' in request.POST.keys():
                payment_form = CIPaymentForm(request.POST, registration_levels=registration.registration_level.upgrades)
                if payment_form.is_valid():
                    upgrade = payment_form.cleaned_data['registration_level']
                    # Cash includes cards swiped on Square
                    method = PaymentMethod.objects.get(name='Cash')
                    payment = Payment(registration=registration,
                                      payment_method=method,
                                      payment_amount=upgrade.price,
                                      payment_level_comment=upgrade.upgrade_registration_level.title + ' Upgrade',
                                      created_by=request.user)
                    payment.save()
                    # Then update the registration
                    registration.registration_level = upgrade.upgrade_registration_level
                    # Mark badge as needing reprint if it already has been printed
                    if registration.needs_print != 1:
                        registration.needs_print = 2
                        messages.warning(request,
                                         'Registration for {} upgraded to {}. Destroy old badge and print a new one.'.format(
                                             registration.badge_name,
                                             registration.registration_level.title
                                         ))
                    else:
                        messages.success(request, 'Registration for {} upgraded to {}.'.format(registration.badge_name))
                    registration.save()
                    LogEntry.objects.log_action(
                        user_id=request.user.pk,
                        content_type_id=ContentType.objects.get_for_model(registration).pk,
                        object_id=registration.pk,
                        object_repr=force_str(registration),
                        action_flag=CHANGE,
                        change_message='Payment taken for on-site upgrade to {0}.'.format(
                            upgrade.upgrade_registration_level.title)
                    )
                    return redirect('convention_check_in', registration.id)
            return render(request, 'registration/check_in/upgrade.html', {'reg': registration,
                                                                      'form': payment_form,
                                                                      'settings': regci_settings,
                                                                      'swag_stats': swag_stats,
                                                                      'reg_lead': reg_lead})


@user_passes_test(lambda u: (u.is_staff and u.is_superuser) or
                            (u.is_staff and u.groups.filter(name='registration').exists()) or
                            (u.is_staff and u.groups.filter(name__in=['reglead', 'constore', 'ops']).exists()) or
                            (u.groups.filter(name='regraf').exists()))
def badge_puller(request, registration_id=None):
    """On-site registration check in, badge puller view
       registration_id: When badge is found, remove from queue
    """

    # Additional group-based security checks
    if request.user.groups.filter(name='regraf').exists():
        # Only allow check-in for rank-and-file reg members to be done
        # on an authorized terminal.
        # Note above that these staffers don't need is_staff set, which
        # additionally keeps them entirely out of the admin.
        if not request.get_signed_cookie('terminal-auth', default=False, salt='terminal-auth'):
            return redirect('home')

    # Only process for the currently active convention
    current_convention = Convention.objects.current()

    queue_name = 'readybadge'

    if registration_id:
        # Find registration, remove from queue
        registration = get_object_or_404(Registration.all_registrations, id=registration_id, \
                                         registration_level__convention=current_convention)

        RegistrationQueue.dequeue(registration, queue_name)

        if request.accepts('text/html'):
            return redirect('convention_badge_puller')

    # Fetch the top 10 items in the queue
    queued_registrations = []
    registration_queue = RegistrationQueue.objects.filter(queue_name=queue_name)[:10]

    # 'touch' each queue item, and add to the list
    for queue_item in registration_queue:
        queue_item.check_visible()
        queued_registrations.append(queue_item.registration)

    if request.accepts('text/html'):
        return render(request, 'registration/check_in/badge_puller.html',
                      {
                          'queued_registrations': queued_registrations,
                          'queue': queue_name,
                      })
    else:
        queued_registrations_json = {'queued_registrations': [
                {
                    'id': reg.id,
                    'badge_name': reg.badge_name,
                    'reg_level': reg.registration_level.title,
                    'status': reg.get_status_display(),
                    'badge_number': reg.badge_number(),
                } for reg in queued_registrations
            ]}
        return JsonResponse(queued_registrations_json)



def avatar_thumbnail(request, avatar_type, avatar_id, maxwidth, maxheight):
    """Process an image server-side to produce a smaller version"""

    query = {'id': avatar_id}
    # Supports either saved registration avatars, or the temp uploads
    if avatar_type == 'r':
        image_model = Registration.all_registrations
    # Public-facing external_id
    elif avatar_type == 'e':
        image_model = Registration.all_registrations
        query = {'external_id': avatar_id}
    # Mid-registration temporary upload
    elif avatar_type == 't':
        image_model = RegistrationTempAvatar
        if int(avatar_id) != request.session.get('avatar', 0) and int(avatar_id) != request.session.get(
                'avatar_original', 0):
            raise Http404()
    # Reg change avatar upload, be a little more forgiving since we clear the session vars
    elif avatar_type == 'c':
        image_model = RegistrationTempAvatar
    else:
        raise Http404()

    thumbnail_size = (int(maxwidth), int(maxheight))

    avatar_object = get_object_or_404(image_model, **query)
    # In both cases the field name is "avatar"
    if not avatar_object.avatar:
        raise Http404()
    image_obj = Image.open(avatar_object.avatar)
    image_obj.thumbnail(thumbnail_size, Image.BICUBIC)

    thumbnail = BytesIO()
    image_obj.convert('RGBA').save(thumbnail, format='png')

    return HttpResponse(thumbnail.getvalue(), content_type='image/png')


@user_passes_test(lambda u: u.is_staff)
def registration_qrcode(request, badge_number):
    """Create a QR code image of a badge ID"""

    # Last two digits of year is the indicator
    current_convention = Convention.objects.current()
    year_indicator = current_convention.name[-2:]
    template = "https://motorcityfurrycon.org/{year_indicator}#{badge_number}"
    # Be sure to set up a redirect to the schedule or some such
    img = qrcode.make(template.format(year_indicator=year_indicator,
                                      badge_number=badge_number))

    response = HttpResponse(content_type='image/png')
    img.save(response, 'PNG')
    return response


def staff_page(request):
    """Generates the staff list based on staff registration objects"""

    current_convention = Convention.objects.current()
    staff_list = StaffRegistration.objects.filter(
        approved=True, convention=current_convention,
    ).exclude(positions__exact='').exclude(positions__isnull=True)

    staff_list_sorted = sorted(staff_list, key=lambda x: (x.sort_order, x.name))

    return render(request, 'staff.html', {
        'staff_list': staff_list_sorted,
    })


@cache_control(max_age=60 * 60 * 24)
def staff_page_image(request, avatar_virtual_filename):
    current_convention = Convention.objects.current()
    staff_object = get_object_or_404(StaffRegistration, convention=current_convention,
                                     avatar_virtual_filename=avatar_virtual_filename)
    if not staff_object.avatar:
        if staff_object.sort_order < 5:
            image_size = (150, 150)
        else:
            image_size = (90, 90)
        im = Image.new('RGBA', image_size)

        mask = Image.open('static/images/staff/mask.png')
        overlay = Image.open('static/images/staff/overlay.png')
        if staff_object.registration.avatar:
            avatar = Image.open(staff_object.registration.avatar)
        else:
            avatar = Image.open('static/images/staff/generic.png')

        # Compose staff image
        im.paste(avatar.resize(image_size, Image.LANCZOS), None, mask.resize(image_size, Image.LANCZOS))
        im.paste(overlay.resize(image_size, Image.LANCZOS), None, overlay.resize(image_size, Image.LANCZOS))

        im_output = BytesIO()
        im.save(im_output, format='png')

        # Cache resulting image
        im_output.seek(0)
        staff_object.avatar.save('{}.png'.format(avatar_virtual_filename),
                                 File(im_output))

        return HttpResponse(im_output.getvalue(), content_type='image/png')

    # Otherwise just grab the file and send it
    return HttpResponse(staff_object.avatar.read(), content_type='image/png')


@user_passes_test(lambda u: u.is_staff and (u.is_superuser or u.groups.filter(name='registration').exists()))
def staff_badge_image(request, registration_id):
    """Composes this year's staff badge"""

    reg = Registration.objects.get(id=registration_id)
    if not 'Staff' in reg.registration_level.title:
        raise PermissionDenied()

    badge_size = (675, 1050)
    avatar_size = (240, 240)
    im = Image.new('RGBA', badge_size, color='white')


    # Add transparent overlay image
    overlay = Image.open('static/images/badges/2024-staff.png').convert("RGBA")
    im.paste(overlay.resize(badge_size, Image.LANCZOS), None, overlay.resize(badge_size, Image.LANCZOS))

    # Overlay uploaded avatar image
    if reg.avatar:
        badge_avatar = Image.open(reg.avatar)
    else:
        badge_avatar = Image.open('static/images/badges/Staff_Generic.png')
    # Calculate centered position on badge
    position = (30, 770)
    im.paste(badge_avatar.resize(avatar_size, Image.BICUBIC), position)

    # Add name and position text
    layer = ImageDraw.Draw(im)
    fontsize = 92
    font = ImageFont.truetype('static/fonts/jp.ttf', fontsize)
    # Write in name
    text = reg.badge_name
    textsize = layer.textsize(text, font=font)
    while textsize[0] > 440 and fontsize > 18:
        fontsize -= 12
        textsize = layer.textsize(text, font=font)
    # Calculate centered position on badge
    position = ((badge_size[0] - textsize[0] - 250) // 2 + 250, 765 - textsize[1])
    layer.text(position, text, fill=0, font=font)
    # layer.rectangle((position, (position[0] + textsize[0], position[1] + textsize[1])), outline="white")
    # Staff badge number, not doing position this year
    font = ImageFont.truetype('static/fonts/GemunuLibre-Bold.ttf', 40)
    badge_number = 'LEETHAXOR' if reg.badge_name == 'Syn' else "2024 - " + str(reg.badge_number())
    textsize = layer.textsize(badge_number, font=font)
    # Calculate centered position on badge
    position = ((badge_size[0] - textsize[0] - 250) // 2 + 250, 815 - textsize[1])
    layer.text(position, str(badge_number), fill=0, font=font)
    # layer.rectangle((position, (position[0] + textsize[0], position[1] + textsize[1])), outline="white")

    # No overlay this year, but leaving the code for easier activation later
    # overlay = Image.open('static/images/badges/StaffTemplate_Overlay.png')
    # im.paste(overlay.resize(badge_size, Image.ANTIALIAS), None, overlay.resize(badge_size, Image.ANTIALIAS))

    # Rotate to print vertical badge in the horizontal template
    im = im.transpose(Image.ROTATE_90)

    im_output = BytesIO()
    im.convert('RGB').save(im_output, format='png')

    return HttpResponse(im_output.getvalue(), content_type='image/png')
