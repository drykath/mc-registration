from django.db import models

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.urls import reverse
from django.utils import timezone
from django.utils.safestring import mark_safe

from convention import get_convention_model
from django.contrib.auth import get_user_model
import json

Convention = get_convention_model()

class RegistrationSettings(models.Model):
    convention = models.OneToOneField(Convention, on_delete=models.CASCADE)
    BADGE_NUMBER_STYLES = (
        (1, 'Assigned when printed'),
        (2, 'Assigned at registration'),
    )
    registration_open = models.BooleanField(default=True)
    badge_number_style = models.IntegerField(default=2, choices=BADGE_NUMBER_STYLES)
    badge_offset = models.IntegerField(default=0)

    def __str__(self):
        return '{} Settings'.format(self.convention.name)

    class Meta:
        verbose_name = 'Registration Settings'
        verbose_name_plural = 'Registration Settings'


class RegistrationActiveManager(models.Manager):
    """By default, only return current convention's active registrations."""

    # TODO: Double check how this should handle yet unpaid cash reg and such.
    # Or payments in progress. Because of the confirmation page, other places it's used.
    def get_queryset(self):
        registrations = super(RegistrationActiveManager, self).get_queryset() \
            .filter(status=1)
        # Try to determine current convention, may return None if unable to
        convention = Convention.objects.current()
        if convention:
            registrations = registrations.filter(
                registration_level__convention=convention,
            )
        return registrations


class Registration(models.Model):
    external_id = models.CharField(max_length=20, blank=True, null=True, unique=True, verbose_name='Confirmation code')
    user = models.ForeignKey(get_user_model(), null=True, blank=True, on_delete=models.SET_NULL)
    registration_date = models.DateTimeField(null=True, blank=True, auto_now_add=True)
    first_name = models.CharField(max_length=255)
    last_name = models.CharField(max_length=255)
    badge_name = models.CharField(max_length=32)
    email = models.EmailField()
    email_me = models.BooleanField(default=False,
                                   verbose_name='Opt-in for occasional email communication from the convention')
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=255)
    state = models.CharField(max_length=255)
    postal_code = models.CharField(max_length=255)
    country = models.CharField(max_length=255)
    birthday = models.DateField()
    registration_level = models.ForeignKey('RegistrationLevel', on_delete=models.PROTECT)
    dealer_registration_level = models.ForeignKey('DealerRegistrationLevel', verbose_name='Dealer Tables', blank=True,
                                                  null=True, on_delete=models.PROTECT)
    shirt_size = models.ForeignKey('ShirtSize', on_delete=models.PROTECT)
    volunteer = models.BooleanField(default=False, verbose_name='Contact me for volunteering opportunities')
    volunteer_phone = models.CharField(max_length=20, blank=True,
                                       verbose_name='Phone Number (only required if volunteering)')
    q1 = models.CharField(max_length=255, blank=True, null=True)
    q2 = models.CharField(max_length=255, blank=True, null=True)
    room_number = models.IntegerField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    private_notes = models.TextField(blank=True, null=True)
    private_check_in = models.BooleanField(default=False)
    checked_in = models.BooleanField(default=False)
    checked_in_on = models.DateTimeField(null=True, blank=True, verbose_name='When checked in')
    STATUS_OPTIONS = (
        (0, 'Unpaid'),
        (1, 'Paid'),
        (2, 'Payment In Progress'),
        (3, 'Refunded'),
        (4, 'Reg Rejected'),
    )
    status = models.IntegerField(default=0, choices=STATUS_OPTIONS)
    swag = models.ManyToManyField('Swag', through='RegistrationSwag')
    NEEDS_PRINT_REASONS = (
        (0, 'No, printed'),
        (1, 'Yes, new reg'),
        (2, 'Yes, upgraded'),
    )
    needs_print = models.IntegerField(default=1, choices=NEEDS_PRINT_REASONS, verbose_name='Badge needs printed?')
    reported_on = models.DateTimeField(null=True, blank=True, verbose_name='Included in paper report on')
    ip = models.GenericIPAddressField()
    avatar = models.ImageField(upload_to='reg_avatars/', null=True, blank=True)
    emergency_contact = models.CharField(max_length=255, blank=True, null=True,
                                         help_text='Optionally, provide an contact person in case of emergency')

    objects = RegistrationActiveManager()
    all_registrations = models.Manager()

    def _get_full_name(self, last_first=False):
        if last_first:
            name_format = '{1}, {0}'
        else:
            name_format = '{0} {1}'
        return name_format.format(self.first_name, self.last_name)

    name = property(_get_full_name)

    def paid(self):
        return self.status == 1

    paid.boolean = True
    paid.short_description = 'Paid?'

    def verify(self):
        coupon = None
        try:
            coupon_use = CouponUse.objects.get(registration=self)
            coupon = coupon_use.coupon
            if (coupon and ((coupon.percent and coupon.discount == 100) or
                            (coupon.percent == False and coupon.discount == self.registration_level.price))):
                return True
        except ObjectDoesNotExist:
            pass

        try:
            payments = Payment.objects.filter(registration=self, refunded_by=None)
            payment_amount = 0
            for payment in payments:
                payment_amount += payment.payment_amount
            if (payment_amount >= self.registration_level.price):
                return True
            if (coupon and ((coupon.percent and ((
                                                         self.registration_level.price * coupon.discount) + payment_amount) >= self.registration_level.price) or
                            (coupon.percent == False and (
                                    (payment_amount + coupon.discount) >= self.registration_level.price)))):
                return True
        except ObjectDoesNotExist:
            pass

        return False

    verify.boolean = True
    verify.short_description = 'Verify Payments'

    def badge_number(self):
        if self.registration_level.convention.registrationsettings.badge_number_style == 1:
            badges = self.badgeassignment_set.order_by('-id')
            if (badges.count() >= 1):
                return '{0:05d}'.format(badges[0].id)
            return None
        elif self.registration_level.convention.registrationsettings.badge_number_style == 2:
            if self.needs_print == 1:
                return None
            return '{0:05d}'.format(self.id - self.registration_level.convention.registrationsettings.badge_offset)

    def avatar_preview(self):
        # return u'<img src="{0}">'.format(self.avatar.url)
        if self.avatar:
            return mark_safe('<img src="{0}">'.format(reverse('avatar_thumbnail', args=[
                'r', self.id, 200, 200
            ])))
        else:
            return '-'

    avatar_preview.short_description = 'Avatar Preview'

    def __str__(self):
        return self.name + ' [' + self.badge_name + ']'


class RegistrationSwag(models.Model):
    swag = models.ForeignKey('Swag', on_delete=models.PROTECT)
    registration = models.ForeignKey(Registration, on_delete=models.CASCADE)
    received = models.BooleanField(default=True)
    size = models.ForeignKey('ShirtSize', on_delete=models.PROTECT, null=True, blank=True)
    backordered = models.BooleanField(default=False)
    backorder_comment = models.CharField(blank=True, null=True, max_length=255)

    def __str__(self):
        return '{0} ({1})'.format(self.swag.description,
                                  'Backordered' if self.backordered else 'Received' if self.received else 'Not received')


class RegistrationHold(models.Model):
    first_name = models.CharField(blank=True, null=True, max_length=255)
    last_name = models.CharField(blank=True, null=True, max_length=255)
    badge_name = models.CharField(blank=True, null=True, max_length=32)
    email = models.EmailField(blank=True, null=True)
    address = models.CharField(blank=True, null=True, max_length=255)
    city = models.CharField(blank=True, null=True, max_length=255)
    state = models.CharField(blank=True, null=True, max_length=255)
    postal_code = models.CharField(blank=True, null=True, max_length=255)
    birthday = models.DateField(blank=True, null=True)
    ip = models.GenericIPAddressField(blank=True, null=True)
    notes_addition = models.TextField(blank=True, null=True)
    private_notes_addition = models.TextField(blank=True, null=True)
    private_check_in = models.BooleanField(default=False)
    notify_registration_group = models.BooleanField(default=True)
    notify_board_group = models.BooleanField(default=False)

    def __str__(self):
        fields = ['first_name', 'last_name', 'badge_name', 'email',
                  'address', 'city', 'state', 'postal_code', 'birthday']
        values = [str(getattr(self, field)) for field in fields if getattr(self, field, False)]
        return ' / '.join(values)


class RegistrationQueue(models.Model):
    # Represents an ordered queue of registrations, which may be useful
    # in speeding up reg lines by having a line wrangler at the end
    # doing an initial look-up. Or if doing at-con delivery.
    queue_name = models.CharField(max_length=15)
    registration = models.OneToOneField(Registration, on_delete=models.CASCADE)
    added = models.DateTimeField(auto_now_add=True)
    top_of_queue = models.DateTimeField(blank=True, null=True)
    additional_data = models.CharField(max_length=40)

    class Meta:
        indexes = [
            models.Index(fields=['queue_name', 'id']),
            models.Index(fields=['registration']),
        ]
        # Always explicitly order by id
        ordering = ['id']

    def check_visible(self):
        # If something has been visible on the screen for maybe 5 or so
        # minutes, expire the item. Assume someone's stepped out of line
        # or otherwise won't be appearing right then.
        if not self.top_of_queue:
            self.top_of_queue = timezone.now()
            self.save()
        else:
            if (timezone.now() - self.top_of_queue).total_seconds() > (5 * 60):
                self.delete()

    @classmethod
    def dequeue(cls, registration, queue_name=None):
        if queue_name:
            cls.objects.filter(registration=registration, queue_name=queue_name).delete()
        else:
            cls.objects.filter(registration=registration).delete()

    @classmethod
    def enqueue(cls, registration, queue_name, preserve=False, additional_data=''):
        # Try to de-queue the registration first, if perhaps moving the
        # reg from one queue to another.
        if not preserve:
            cls.dequeue(registration)
        q = cls(queue_name=queue_name, registration=registration, additional_data='')
        q.save()


class RegistrationTempAvatar(models.Model):
    """Used primarily for image uploads during registration.
       May also be used for badge renames."""

    avatar = models.ImageField(upload_to='tmp_avatars/', null=True, blank=True)
    new_badge_name = models.CharField(max_length=32, null=True, blank=True)
    uploaded = models.DateTimeField(auto_now_add=True)

    # Delete the temp upload along with this object
    def delete(self, using=None):
        name = self.avatar.name
        super(RegistrationTempAvatar, self).delete(using)
        if name:
            self.avatar.storage.delete(name)


class StaffRegistration(models.Model):
    """Augments Registration model for staff members"""

    convention = models.ForeignKey(Convention, on_delete=models.CASCADE)
    registration = models.ForeignKey(Registration, on_delete=models.CASCADE)
    name_override = models.CharField(blank=True, null=True, max_length=32)
    positions = models.CharField(blank=True, null=True, max_length=255)
    SORT_ORDER_CHOICES = (
        (1, 'Chair'),
        (2, 'Vice-chair'),
        (3, 'Treasurer'),
        (4, 'Board'),
        (5, 'Staff'),
    )
    sort_order = models.IntegerField(default=5, choices=SORT_ORDER_CHOICES)
    avatar_virtual_filename = models.CharField(blank=True, null=True, max_length=50)
    avatar = models.ImageField(upload_to='staff_avatars/', null=True, blank=True)
    approved = models.BooleanField(default=False)
    extra = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['sort_order']

    def _get_badge_name(self):
        return self.name_override or self.registration.badge_name

    name = property(_get_badge_name)

    def _get_extra_data(self):
        try:
            return json.loads(self.extra)
        except ValueError:
            return None

    extra_data = property(_get_extra_data)

    # Delete the processed image along with this object
    def delete(self, using=None):
        if self.avatar:
            name = self.avatar.name
            super(RegistrationTempAvatar, self).delete(using)
            self.avatar.storage.delete(name)

    def __str__(self):
        return self.get_sort_order_display() + ' [' + self.name + ']' + (' Unapproved' if not self.approved else '')


class BadgeAssignment(models.Model):
    registration = models.ForeignKey('Registration', on_delete=models.CASCADE)
    printed_by = models.ForeignKey(get_user_model(), related_name="printed_by_user", on_delete=models.PROTECT)
    printed_on = models.DateTimeField(auto_now_add=True)
    registration_level = models.ForeignKey('RegistrationLevel', null=True, blank=True, on_delete=models.PROTECT)

    def __str__(self):
        return self.registration.badge_name + ' [' + "%05d" % (self.id) + ']'


class ShirtSize(models.Model):
    seq = models.IntegerField()
    size = models.CharField(max_length=20)

    def __str__(self):
        return self.size


class Payment(models.Model):
    registration = models.ForeignKey('Registration', on_delete=models.CASCADE)
    PAYMENT_STATES = (
        (1, 'Paid'),
        (2, 'Refund Requested'),
        (3, 'Refunded'),
    )
    payment_state = models.IntegerField(default=1, choices=PAYMENT_STATES)
    payment_method = models.ForeignKey('PaymentMethod', on_delete=models.PROTECT)
    payment_level_comment = models.CharField(max_length=255, blank=True, null=True)
    payment_amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_received = models.DateTimeField(auto_now_add=True)
    payment_extra = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    created_by = models.ForeignKey(get_user_model(), null=True, related_name="created_by_user", on_delete=models.SET_NULL)
    refunded_by = models.ForeignKey(get_user_model(), blank=True, null=True, related_name="refunded_by_user",
                                    on_delete=models.PROTECT)
    refund_reason = models.CharField(max_length=255, blank=True, null=True)
    refund_requested = models.DateTimeField(blank=True, null=True)
    refund_processed = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return self.registration.name + ' [' + "%.02f" % (self.payment_amount) + ']'


class RegistrationLevelCurrentManager(models.Manager):
    """Return current convention's active levels."""

    def get_queryset(self):
        levels = super(RegistrationLevelCurrentManager, self).get_queryset() \
            .filter(active=True) \
            .exclude(opens__gt=timezone.now()) \
            .exclude(deadline__lt=timezone.now())
        # Try to determine current convention, may return None if unable to
        convention = Convention.objects.current()
        if convention:
            levels = levels.filter(
                convention=convention,
            )
        return levels


class RegistrationLevel(models.Model):
    seq = models.IntegerField()
    convention = models.ForeignKey(Convention, on_delete=models.CASCADE)
    limit = models.IntegerField()
    title = models.CharField(max_length=255)
    description = models.TextField()
    background = models.CharField(max_length=255)
    added_text = models.CharField(max_length=255, null=True, blank=True)
    color = models.CharField(max_length=7)
    opens = models.DateTimeField(null=True, blank=True)
    deadline = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    swag = models.ManyToManyField('Swag', through='RegistrationLevelSwag')

    objects = models.Manager()
    current = RegistrationLevelCurrentManager()

    def _get_current_price(self):
        current_active_price = self.registrationlevelprice_set.filter(
            active_date__lt=timezone.now()
        ).order_by('-active_date').first()
        if current_active_price:
            return current_active_price.price
        else:
            return None

    price = property(_get_current_price)

    def __str__(self):
        return '{0} [{1}]'.format(self.title, self.convention.name)


class RegistrationLevelPrice(models.Model):
    registration_level = models.ForeignKey(RegistrationLevel, on_delete=models.CASCADE)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    active_date = models.DateTimeField()

    def __str__(self):
        return '{0} as of {1}'.format(self.price, self.active_date)


class RegistrationLevelSwag(models.Model):
    swag = models.ForeignKey('Swag', on_delete=models.PROTECT)
    registration_level = models.ForeignKey(RegistrationLevel, on_delete=models.CASCADE)
    must_register_before = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return '{0}: {1}'.format(self.registration_level.title, self.swag.description)


class Swag(models.Model):
    convention = models.ForeignKey(Convention, on_delete=models.CASCADE)
    description = models.CharField(max_length=100)
    sizes = models.BooleanField(default=False)

    def __str__(self):
        return self.description


class DealerRegistrationLevel(models.Model):
    convention = models.ForeignKey(Convention, on_delete=models.CASCADE)
    title = models.CharField(max_length=100, null=True, blank=True)
    number_tables = models.IntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return str(self.number_tables) + ' [' + "%.02f" % self.price + ']'


class CouponCode(models.Model):
    convention = models.ForeignKey(Convention, on_delete=models.CASCADE)
    code = models.CharField(max_length=255)
    discription = models.CharField(max_length=255, null=True, blank=True)
    discount = models.DecimalField(max_digits=10, decimal_places=2)
    percent = models.BooleanField(default=False)
    single_use = models.BooleanField(default=False)
    force_registration_level = models.ForeignKey('RegistrationLevel', null=True, blank=True, on_delete=models.PROTECT)
    force_dealer_registration_level = models.ForeignKey('DealerRegistrationLevel', null=True, blank=True,
                                                        on_delete=models.PROTECT)

    def _get_use_count(self):
        return self.couponuse_set.count()

    use_count = property(_get_use_count)

    def __str__(self):
        if self.percent:
            return self.code + ' [' + "%d%%" % self.discount + ']'
        else:
            return self.code + ' [' + "%.02f" % self.discount + ']'


class CouponUse(models.Model):
    registration = models.ForeignKey('Registration', on_delete=models.CASCADE)
    coupon = models.ForeignKey('CouponCode', on_delete=models.CASCADE)

    def __str__(self):
        return '%s - %s' % (self.registration, self.coupon)


class PaymentMethod(models.Model):
    seq = models.IntegerField()
    name = models.CharField(max_length=255)
    active = models.BooleanField(default=True)
    is_credit = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class RegistrationUpgrade(models.Model):
    current_registration_level = models.ForeignKey(RegistrationLevel, related_name='upgrades', on_delete=models.CASCADE)
    upgrade_registration_level = models.ForeignKey(RegistrationLevel, related_name='upgrades_from',
                                                   on_delete=models.CASCADE)
    description = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    def _get_current_price(self):
        current_active_price = self.registrationupgradeprice_set.filter(
            active_date__lt=timezone.now()
        ).order_by('-active_date').first()
        if current_active_price:
            return current_active_price.price
        else:
            return None

    price = property(_get_current_price)

    # TODO: Save validation, make sure registration_levels point to same convention
    def __str__(self):
        return self.current_registration_level.title + \
            ' to ' + self.upgrade_registration_level.title

    def clean(self):
        # Only allow upgrades within the same convention
        if self.current_registration_level.convention != self.upgrade_registration_level.convention:
            raise ValidationError('Upgrades only allowed within the same convention.')


class RegistrationUpgradePrice(models.Model):
    registration_upgrade = models.ForeignKey(RegistrationUpgrade, on_delete=models.CASCADE)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    active_date = models.DateTimeField()

    def __str__(self):
        return '{0} as of {1}'.format(self.price, self.active_date)
