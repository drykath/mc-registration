from django.contrib import admin
from django.contrib import messages
from django.contrib.admin.helpers import ActionForm
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.shortcuts import render
from django.urls import path, resolve, reverse
from django.utils.safestring import mark_safe
from django import forms

from datetime import datetime

from convention import get_convention_model
Convention = get_convention_model()

from . import models

class RegistrationAdminForm(ActionForm):
    amount = forms.FloatField(widget=forms.NumberInput(attrs={'style': 'width:auto'}), required=False)
    method = forms.ModelChoiceField(widget=forms.Select(attrs={'style': 'width:auto'}), empty_label=None, queryset=models.PaymentMethod.objects.order_by('seq'), required=False)
    reprint = forms.BooleanField(required=False)


class RegistrationModelAdmin(admin.ModelAdmin):
    def formfield_for_dbfield(self, *args, **kwargs):
        from django.forms.models import ModelChoiceField
        formfield = super().formfield_for_dbfield(*args, **kwargs)

        # For foreign key fields, hide direct edit ability
        #from django.forms.fields import ForeignKeyField
        if isinstance(formfield, ModelChoiceField):
            formfield.widget.can_delete_related = False
            formfield.widget.can_change_related = False
            formfield.widget.can_add_related = False
            formfield.widget.can_view_related = False

        return formfield


class PaymentInline(admin.TabularInline):
    model = models.Payment
    can_delete = False
    extra = 0
    max_num = 0
    readonly_fields = ['payment_method', 'payment_amount', 'payment_received', 'payment_extra',
                       'created_by', 'payment_state', 'refunded_by']


class RegistrationAdmin(RegistrationModelAdmin):
    list_display = ('badge_name', 'first_name', 'last_name', 'registration_level', 'shirt_size', 'checked_in', 'status', 'badge_number')
    list_filter = ('registration_level__convention', 'registration_level__title', 'checked_in', 'needs_print', 'status', 'shirt_size', 'volunteer', 'payment__payment_received', 'payment__payment_method')
    search_fields = ['first_name', 'last_name', 'badge_name', 'email', 'badgeassignment__id', 'external_id']
    autocomplete_fields = ['user']
    actions = ['mark_checked_in', 'apply_payment', 'refund_payment', 'undo_refund_payment', 'print_badge', 'link_as_staff', 'download_registration_detail']
    action_form = RegistrationAdminForm
    ordering = ('id',)
    inlines = [PaymentInline]
    readonly_fields = ( 'external_id', 'confirmation_link', 'avatar_preview', )

    filter_convention = None

    def get_convention_from_request(self, request):
        resolved = resolve(request.path_info)
        if 'object_id' in resolved.kwargs:
            return Registration.all_registrations.get(pk=resolved.kwargs['object_id']).registration_level.convention

    def get_queryset(self, request):
        # Default manager excludes status<>1; show those
        qs = self.model.all_registrations.get_queryset()

        ordering = self.ordering or ()
        if ordering:
            qs = qs.order_by(*ordering)

        return qs

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'registration_level':
            convention = self.get_convention_from_request(request)
            if convention:
                kwargs['queryset'] = RegistrationLevel.objects.filter(convention=convention)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_urls(self):
        urls = super(RegistrationAdmin, self).get_urls()
        my_urls = [
            path('print/', self.print_badge_list, name='print')
        ]
        return my_urls + urls

    def confirmation_link(self, obj):
        link = reverse('convention_confirm', args=[obj.external_id])
        html = '<a href="https://{domain}{link}">https://{domain}{link}</a>'.format(
                domain=obj.registration_level.convention.site.domain,
                link=link
                )
        return mark_safe(html)

    def mark_checked_in(self, request, queryset):
        for id in queryset:
            if id.registration_level.convention != Convention.objects.current():
                self.message_user(request, 'Cannot check in reg %s from a different year' % (id))
            else:
                id.checked_in = True
                id.checked_in_on = datetime.now()
                id.save()
                self.log_change(request, id, 'Checked in attendee')
                self.message_user(request, '%s successfully checked in!' % id)
    mark_checked_in.short_description = 'Check in attendee'

    def apply_payment(self, request, queryset):
        try: 
            amount = request.POST['amount']
            method = models.PaymentMethod.objects.get(id=request.POST['method'])
        except ObjectDoesNotExist:
            method = None
        if (method and amount):
            for id in queryset:
                if id.registration_level.convention != Convention.objects.current():
                    self.message_user(request, 'Cannot apply payment to reg %s from a different year' % (id))
                    continue
                payment = Payment(registration=id,
                                  payment_method=method,
                                  payment_amount=float(amount),
                                  created_by=request.user)
                payment.save()
                self.log_change(request, id, 'Applied %.02f payment by %s' % (float(amount), method))
                self.message_user(request, 'Applied %.02f payment by %s to %s' % (float(amount), method, id))
        else:
            self.message_user(request, 'Must specify an amount and payment method!', messages.ERROR)

    def refund_payment(self, request, queryset):
        for id in queryset:
            if id.registration_level.convention != Convention.objects.current():
                self.message_user(request, 'Cannot refund reg %s from a different year' % (id))
                continue
            if id.checked_in:
                self.message_user(request, 'Cannot refund checked-in reg %s' % (id))
                continue
            payments = Payment.objects.filter(registration=id)
            for payment in payments:
                if (payment.payment_method.is_credit and payment.payment_extra):
                    if payment.payment_state == 1:
                        payment.payment_state = 2
                        payment.refund_requested = timezone.now()
                        payment.refunded_by = request.user
                        payment.save()
                        self.message_user(request, 'Requested a refunded of %.02f by %s to %s' % (payment.payment_amount, payment.payment_method, id))
                    elif payment.payment_state == 2:
                        self.message_user(request, 'Refund for %s already requested' % (id))
                    elif payment.payment_state == 3:
                        self.message_user(request, 'Refund for %s already processed' % (id))
                else:
                    payment.payment_state = 3
                    payment.refunded_by = request.user
                    payment.refund_processed = timezone.now()
                    payment.save()
                    self.message_user(request, 'Marked refunded %.02f payment by %s to %s' % (payment.payment_amount, payment.payment_method, id))
            id.status = 3
            id.save()
            self.log_change(request, id, 'Payment refund requested and registration marked as not paid.')
    refund_payment.short_description = 'Refund all payments from attendee'

    def undo_refund_payment(self, request, queryset):
        for id in queryset:
            payments = Payment.objects.filter(registration=id)
            for payment in payments:
                if (payment.payment_method.is_credit and payment.payment_extra):
                    if payment.payment_state == 2:
                        payment.payment_state = 1
                        payment.save()
                        self.message_user(request, 'Refund request for %s has been cancelled' % (id))
                        self.log_change(request, id, 'Payment refund request cancelled')
                    elif payment.payment_state == 3:
                        self.message_user(request, 'Refund for %s is already processed. Sorry. :(' % (id))
                        self.log_change(request, id, 'Payment refund request has already been processed and could not be cancelled')
                else:
                    payment.payment_state = 1
                    payment.save()
                    self.message_user(request, 'Unmarked refunded %.02f payment by %s to %s' % (payment.payment_amount, payment.payment_method, id))
                    self.log_change(request, id, 'Non-credit payment refund reversed')
    undo_refund_payment.short_description = 'Attempt reversal of all refunded payments from attendee'

    def print_badge(self, request, queryset):
        printable = True
        ac = transaction.get_autocommit()
        transaction.set_autocommit(False)
        for reg in queryset:
            if reg.registration_level.convention != Convention.objects.current():
                self.message_user(request, 'Cannot print badge %s from a different year' % (reg))
                printable = False
            elif not reg.paid():
                self.message_user(request, 'Cannot print unpaid badge for %s' % (reg), messages.ERROR)
            badge_number = None
            reprint = request.POST.get('reprint')
            if reprint:
                badge_number = reg.badge_number()
            if not reprint or not badge_number:
                badge = BadgeAssignment(registration=reg, printed_by=request.user, registration_level=reg.registration_level)
                badge.save()
                badge_number = badge.id
            # Mark the badge as not needing printed any longer
            reg.needs_print = 0
            reg.save()
            #reg.badge_number = '%05d' % int(badge_number)
            self.log_change(request, reg,
                'Badge printed and assigned number %05d' % int(badge_number))
        if printable:
            transaction.commit()
            transaction.set_autocommit(ac)
            return render(request, 'register/badge.html', {'badges': queryset})
        else:
            transaction.rollback()
            transaction.set_autocommit(ac)

    def print_badge_list(self, request):
        badges = Registration.objects.filter(checked_in=False).order_by('last_name', 'first_name')
        split_badges = []
        temp_list = []
        for badge in badges:
            if badge.badge_number():
                temp_list.append({'name': badge._get_full_name(last_first=True), 'badge_name': badge.badge_name, 'badge_number': badge.badge_number(), 'registration_level': badge.registration_level.title})
            if len(temp_list) == 25:
                split_badges.append({'list': temp_list, 'last': False})
                temp_list = []
        if len(temp_list) > 0:
            split_badges.append({'list': temp_list, 'last': True})
        return render(request, 'register/badgelist.html', {'lists': split_badges})

    def link_as_staff(self, request, queryset):
        for reg in queryset:
            if 'Staff' not in reg.registration_level.title:
                self.message_user(request, '{} is not a staff registration'.format(reg), messages.ERROR)
                continue
            if reg.staffregistration_set.count() > 0:
                self.message_user(request, '{} already linked to Staff Registration'.format(reg), messages.WARNING)
                continue
            sr = StaffRegistration.objects.create(
                convention=reg.registration_level.convention,
                registration=reg,
            )
            self.log_change(request, reg, 'Staff registration link created')
            self.log_change(request, sr, 'Staff registration link created')
            self.message_user(request, '{} linked to Staff Registration'.format(reg))
    link_as_staff.short_description = 'Link as staff'

    def download_registration_detail(self, request, queryset):
        registration_list = []
        for badge in queryset:
            payments = Payment.objects.filter(registration=badge)
            for payment in payments:
                discount_amount = ''
                try:
                    coupon = CouponUse.objects.get(registration=badge)
                    if coupon.coupon.percent:
                        discount_amount = '%.02f' % ((coupon.coupon.discount / 100) * badge.registration_level.price)
                    else:
                        discount_amount = '%.02f' % (coupon.coupon.discount)
                except ObjectDoesNotExist:
                    pass
                registration_list.append({'name': badge._get_full_name(last_first=True),
                                          'email': badge.email,
                                          'address': badge.address,
                                          'city': badge.city,
                                          'state': badge.state,
                                          'postal_code': badge.postal_code,
                                          'country': badge.country,
                                          'badge_name': badge.badge_name.replace('"', '""'),
                                          'badge_number': badge.badge_number(),
                                          'registration_level': badge.registration_level.title.replace('"', '""'),
                                          'payment_registration_level': payment.payment_level_comment.replace('"', '""') if payment.payment_level_comment else '',
                                          'dealer_registration_level': badge.dealer_registration_level.number_tables if badge.dealer_registration_level else '',
                                          'payment_amount': '%.02f' % payment.payment_amount,
                                          'payment_created': payment.payment_received,
                                          'received_by': payment.created_by.username.replace('"', '""') if payment.created_by else '',
                                          'refunded_by': payment.refunded_by.username.replace('"', '""') if payment.refunded_by else '',
                                          'discount_amount': discount_amount,
                                          'payment_method': payment.payment_method})
        if not payments:
            discount_amount = ''
            try:
                coupon = CouponUse.objects.get(registration=badge)
                if coupon.coupon.percent:
                    discount_amount = '%.02f' % ((coupon.coupon.discount / 100) * badge.registration_level.price)
                else:
                    discount_amount = '%.02f' % (coupon.coupon.discount)
            except ObjectDoesNotExist:
                pass
            registration_list.append({'name': badge._get_full_name(last_first=True),
                                      'email': badge.email,
                                      'address': badge.address,
                                      'city': badge.city,
                                      'state': badge.state,
                                      'postal_code': badge.postal_code,
                                      'country': badge.country,
                                      'badge_name': badge.badge_name.replace('"', '""'),
                                      'badge_number': badge.badge_number(),
                                      'registration_level': badge.registration_level.title.replace('"', '""'),
                                      'payment_registration_level': '',
                                      'dealer_registration_level': badge.dealer_registration_level.number_tables if badge.dealer_registration_level else '',
                                      'payment_amount': '0.00',
                                      'payment_created': '',
                                      'received_by': '',
                                      'refunded_by': '',
                                      'discount_amount': discount_amount,
                                      'payment_method': ''})
        response = render(request, 'register/regdetail.csv', {'badges': registration_list}, content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="regdetail.csv"'
        return response

admin.site.register(models.Registration, RegistrationAdmin)

class BadgeAssignmentAdmin(admin.ModelAdmin):
    search_fields = ['id', 'registration__first_name', 'registration__last_name', 'registration__badge_name', 'registration__email']

admin.site.register(models.BadgeAssignment, BadgeAssignmentAdmin)


class StaffRegistrationAdmin(RegistrationModelAdmin):
    list_display = ( '__str__', 'approved', 'registration', 'sort_order', )
    list_filter = ( 'convention', )
    actions = ['mark_approved']

    def mark_approved(self, request, queryset):
        queryset.update(approved=True)
        for id in queryset:
            self.log_change(request, id, 'Approve Staff Registration')
            self.message_user(request, '%s registration approved' % id)
    mark_approved.short_description = 'Approve'

admin.site.register(models.StaffRegistration, StaffRegistrationAdmin)


class RegistrationLevelPriceInline(admin.TabularInline):
    model = models.RegistrationLevelPrice
    can_delete = False
    extra = 1


class RegistrationLevelSwagInline(admin.TabularInline):
    model = models.RegistrationLevelSwag
    extra = 1

    def get_convention_from_request(self, request):
        resolved = resolve(request.path_info)
        if 'object_id' in resolved.kwargs:
            return RegistrationLevel.objects.get(pk=resolved.kwargs['object_id']).convention

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'swag':
            kwargs['queryset'] = Swag.objects.filter(convention=self.get_convention_from_request(request))
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class RegistrationLevelAdmin(RegistrationModelAdmin):
    inlines = [RegistrationLevelPriceInline, RegistrationLevelSwagInline]
    list_display = ( '__str__', 'price', )
    list_filter = ( 'convention', )
    readonly_fields = ( 'price', )

    def price(self, object):
        return object.price

    price.short_description = 'Active Price'

admin.site.register(models.RegistrationLevel, RegistrationLevelAdmin)


class RegistrationUpgradePriceInline(admin.TabularInline):
    model = models.RegistrationUpgradePrice
    can_delete = False
    extra = 1


class RegistrationUpgradeAdmin(RegistrationModelAdmin):
    inlines = [RegistrationUpgradePriceInline]
    list_display = ( '__str__', 'price', )
    readonly_fields = ( 'price', )

    def price(self, object):
        return object.price

    price.short_description = 'Active Price'

admin.site.register(models.RegistrationUpgrade, RegistrationUpgradeAdmin)


class SwagAdmin(RegistrationModelAdmin):
    list_display = ( '__str__', 'sizes', 'convention', )
    list_filter = ( 'convention', )

admin.site.register(models.Swag, SwagAdmin)


class CouponCodeAdmin(admin.ModelAdmin):
    list_display = ( '__str__', 'single_use', 'convention', 'use_count' )
    readonly_fields = ( 'use_count', )
    list_filter = ( 'convention', )

admin.site.register(models.CouponCode, CouponCodeAdmin)


# Other models that don't need customization
admin.site.register(models.DealerRegistrationLevel)
admin.site.register(models.PaymentMethod)
admin.site.register(models.RegistrationHold)
admin.site.register(models.ShirtSize)
admin.site.register(models.RegistrationSettings)
