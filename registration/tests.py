from django.test import TestCase

from datetime import timedelta
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone
from io import BytesIO
from unittest import mock
from PIL import Image
import random
import re

from convention.tests import create_test_convention

from . import models
from .utils import simple_feistel, stringify_integer

# TODO: Form tests

# Test Helpers

def create_test_paymentmethod(name='Cash', is_credit=False):
    return models.PaymentMethod.objects.create(
        seq=0,
        name=name,
        active=True,
        is_credit=is_credit
    )

def create_test_registration(registration_level, **kwargs):
    # Default values for a registration
    defaults = dict(
        user=create_test_user(),
        first_name='Drykath',
        last_name='Dragon',
        badge_name='Drykath',
        email='drykath@example.com',
        address='',
        city='',
        state='',
        postal_code='',
        country='',
        birthday=(timezone.now() - timedelta(days=18*366)).date(),
        shirt_size=create_test_shirtsizes()['small'],
        ip='127.0.0.1'
    )
    # Override with those provided
    defaults.update(kwargs)

    return models.Registration.objects.create(
        registration_level=registration_level,
        **defaults
    )

def create_test_registrationlevels(convention, names=['basic', 'sponsor', 'supersponsor']):
    levels = {}
    seq = 0
    for level in names:
        seq += 1
        levels[level] = models.RegistrationLevel.objects.create(
            seq=seq,
            convention=convention,
            limit=0,
            title=level,
            description=level,
            background='#000',
            added_text='',
            color='#000',
            deadline=timezone.now() + timedelta(days=365),
            active=True
        )
        models.RegistrationLevelPrice.objects.create(
            registration_level=levels[level],
            price=seq,
            active_date=timezone.now() - timedelta(days=365)
        )
    return levels

def create_test_registrationupgrades(levels=[]):
    """Takes an itertable of 2-tuples containing RegistrationLevels"""
    upgrades = []
    for pair in levels:
        upgrade = models.RegistrationUpgrade.objects.create(
            current_registration_level=pair[0],
            upgrade_registration_level=pair[1],
            description='',
            active=True
        )
        models.RegistrationUpgradePrice.objects.create(
            registration_upgrade = upgrade,
            price=pair[1].price - pair[0].price,
            active_date=timezone.now() - timedelta(days=365)
        )
        upgrades.append(upgrade)
    return upgrades

def create_test_shirtsizes(names=['small', 'medium', 'large']):
    sizes = {}
    seq = 0
    for size in names:
        seq += 1
        sizes[size] = models.ShirtSize.objects.create(
            seq=seq,
            size=size,
        )
    return sizes

def create_test_user(username='drykath', password=None, email=None):
    UserModel = get_user_model()
    if not email:
        email = '{0}@{0}.com'.format(username),
    user, created = UserModel.objects.get_or_create(
        username=username,
        defaults={
            'email': email,
        }
    )
    if password:
        user.set_password(password)
        user.save()
    else:
        if created:
            user.set_password(username)
            user.save()
    return user


# Model Tests

class CouponCodeModelTest(TestCase):
    def setUp(self):
        convention = create_test_convention()
        models.CouponCode.objects.create(
            convention=convention,
            code='half-off',
            discount=50,
            percent=True,
        )
        models.CouponCode.objects.create(
            convention=convention,
            code='ten-dollars',
            discount=10,
            percent=False,
        )

    def test_coupon_model_amount(self):
        coupon = models.CouponCode.objects.get(code='ten-dollars')
        self.assertEqual(coupon.__str__(), 'ten-dollars [10.00]')

    def test_coupon_model_percentage(self):
        coupon = models.CouponCode.objects.get(code='half-off')
        self.assertEqual(coupon.__str__(), 'half-off [50%]')


class PaymentMethodModelTest(TestCase):
    def test_payment_method_model_str(self):
        method = create_test_paymentmethod()
        self.assertEqual(str(method), method.name)


class RegistrationHoldModelTest(TestCase):
    def setUp(self):
        self.convention = create_test_convention()
        self.levels = create_test_registrationlevels(self.convention)

    def test_flag_badgename(self):
        # No holds...
        create_test_registration(self.levels['sponsor'], badge_name='Drykath')
        self.assertEqual(len(mail.outbox), 0)

        hold = models.RegistrationHold.objects.create(badge_name='DRYKATH', notes_addition='Test', notify_registration_group=True)
        create_test_registration(self.levels['sponsor'], badge_name='Drykath')
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['registration@yourconvention.org'])

        self.assertEqual(str(hold), 'DRYKATH')

    def test_flag_realname(self):
        hold = models.RegistrationHold.objects.create(first_name='DRYKATH', last_name='DRAGON', notes_addition='Test', notify_registration_group=True)
        # All specified fields must match, if some don't, don't flag registration
        self.reg = create_test_registration(self.levels['sponsor'], first_name='Drykath', last_name='Different')
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.reg.notes, None)
        self.reg = create_test_registration(self.levels['sponsor'], first_name='Drykath', last_name='Dragon')
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue('Test' in self.reg.notes)

        self.assertEqual(str(hold), 'DRYKATH / DRAGON')

    def test_flag_exact(self):
        birthday = (timezone.now() - timedelta(days=18*366)).date()
        models.RegistrationHold.objects.create(birthday=birthday, notes_addition='Test', notify_registration_group=True, notify_board_group=True)
        # All specified fields must match, if some don't, don't flag registration
        create_test_registration(self.levels['sponsor'], birthday=birthday)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue('registration@yourconvention.org' in mail.outbox[0].to)
        self.assertTrue('board@yourconvention.org' in mail.outbox[0].to)

    def test_flag_private(self):
        models.RegistrationHold.objects.create(first_name='DRYKATH', last_name='DRAGON', private_notes_addition='Test', private_check_in=True)
        self.reg = create_test_registration(self.levels['sponsor'], first_name='Drykath', last_name='Dragon')
        self.assertFalse(self.reg.notes)
        self.assertTrue('Test' in self.reg.private_notes)
        self.assertTrue(self.reg.private_check_in)

    def test_flag_duplicate_detection(self):
        self.reg = create_test_registration(self.levels['sponsor'], first_name='Drykath', last_name='Dragon',
            birthday=(timezone.now() - timedelta(days=18*366)).date())
        self.reg.status = 1
        self.reg.save()
        self.assertEqual(self.reg.notes, None)

        # Different enough doesn't flag
        self.reg = create_test_registration(self.levels['sponsor'], first_name='Drykath', last_name='Different')
        self.assertEqual(self.reg.notes, None)

        # If birthday and last_name match, flag it
        self.reg = create_test_registration(self.levels['sponsor'], first_name='Foobar', last_name='Dragon',
            birthday=(timezone.now() - timedelta(days=18*366)).date())
        self.assertTrue('duplicate registration' in self.reg.notes)
        # Or if first_name and last_name match, flag it
        self.reg = create_test_registration(self.levels['sponsor'], first_name='Drykath', last_name='Dragon',
            birthday=(timezone.now() - timedelta(days=19*366)).date())
        self.assertTrue('duplicate registration' in self.reg.notes)


class RegistrationModelTest(TestCase):
    def setUp(self):
        self.convention = create_test_convention()
        self.levels = create_test_registrationlevels(self.convention)
        self.reg = create_test_registration(self.levels['sponsor'])

    def test_registration_name(self):
        reg = self.reg
        self.assertEqual(reg.name, reg.first_name + ' ' + reg.last_name)
        self.assertEqual(reg._get_full_name(last_first=True),
                         reg.last_name + ', ' + reg.first_name)
        self.assertEqual(reg.__str__(),
                         '{0} [{1}]'.format(reg.name, reg.badge_name))

    def test_registration_status_paid(self):
        reg = self.reg
        self.assertEqual(reg.paid(), False)

        # Setting the flag manually triggers the paid() function response
        reg.status = 1
        reg.save()
        self.assertEqual(reg.paid(), True)

    def test_registration_verify_payment(self):
        reg = self.reg
        self.assertEqual(reg.verify(), False)

        # Create a payment record...
        payment = models.Payment.objects.create(
            registration=reg,
            payment_method=create_test_paymentmethod('Cash'),
            payment_amount=self.levels['sponsor'].price,
        )
        self.assertEqual(reg.verify(), True)

    def test_registration_coupon_percent(self):
        reg = self.reg
        self.assertFalse(reg.verify())

        # Create a coupon to use
        coupon = models.CouponCode.objects.create(
            convention=self.convention,
            code='full-discount',
            discount=100,
            percent=True,
        )
        models.CouponUse.objects.create(
            registration=self.reg,
            coupon=coupon
        )
        # Should now verify as valid
        self.assertTrue(reg.verify())

    def test_registration_coupon_combined(self):
        reg = self.reg
        self.assertFalse(reg.verify())

        # Create a coupon to use
        coupon = models.CouponCode.objects.create(
            convention=self.convention,
            code='one-dollar',
            discount=1,
            percent=False,
        )
        models.CouponUse.objects.create(
            registration=self.reg,
            coupon=coupon
        )
        # Need an additional payment to make up the difference
        payment = models.Payment.objects.create(
            registration=reg,
            payment_method=create_test_paymentmethod('Cash'),
            payment_amount=self.levels['sponsor'].price - 1,
        )
        # Should now verify as valid
        self.assertTrue(reg.verify())

    def test_registration_manager_current_convention(self):
        """Test manager default filter on active registrations"""

        # Since the default registration isn't paid
        self.assertEqual(models.Registration.objects.count(), 0)
        self.assertEqual(models.Registration.all_registrations.count(), 1)

        # But it should show up once paid
        self.reg.status = 1
        self.reg.save()
        self.assertEqual(models.Registration.objects.count(), 1)

        # Set up a second convention
        previous_year = create_test_convention('Last Year', site_id=None)
        previous_year_levels = create_test_registrationlevels(previous_year)
        old_registration = create_test_registration(previous_year_levels['sponsor'])

        # Last year's registration shouldn't show up in the default list
        self.assertEqual(models.Registration.objects.count(), 1)
        # But we can query it if needed
        self.assertEqual(models.Registration.all_registrations.count(), 2)

    def test_registration_external_id(self):
        """The external ID is the user-facing ID value"""

        # External ID is present and calculated properly
        self.assertEqual(self.reg.external_id,
            stringify_integer(simple_feistel(self.reg.id)))

    def test_registration_badge_number_style_1(self):
        "Badge numbering style used in 2015-2016"
        self.convention.badge_number_style = 1
        self.convention.save()

        # Old school, no badge number is assigned initially
        self.assertEqual(self.reg.badge_number(), None)

        user = create_test_user()
        # Create a BadgeAssignment
        badge = models.BadgeAssignment.objects.create(
            registration = self.reg,
            printed_by = user,
            registration_level = self.reg.registration_level,
        )
        string_id = '{0:05d}'.format(badge.id)

        self.assertEqual(badge.__str__(),
                '{0} [{1}]'.format(self.reg.badge_name, string_id))

        # Badge number is now latest assignment ID
        self.assertEqual(self.reg.badge_number(), string_id)

        # Which means that a subsequent assignment will be a new number
        badge = models.BadgeAssignment.objects.create(
            registration = self.reg,
            printed_by = user,
            registration_level = self.reg.registration_level,
        )
        string_id = '{0:05d}'.format(badge.id)
        self.assertEqual(self.reg.badge_number(), string_id)

    def test_registration_badge_number_style_2(self):
        "Badge numbering style used in 2017-"
        # Badge number is not displayed initially
        self.assertEqual(self.reg.badge_number(), None)
        # Until it has been printed...
        self.reg.needs_print = 0
        self.reg.save()

        # Our test convention has a default offset of 0
        string_id = '{0:05d}'.format(self.reg.id - self.convention.registrationsettings.badge_offset)

        # Badge number is now the same as the registration ID
        self.assertEqual(self.reg.badge_number(), string_id)

        # But if we set an offset...
        self.convention.registrationsettings.badge_offset = self.reg.id - 1
        self.convention.save()
        # Our badge numbering should start at 1
        string_id = '{0:05d}'.format(1)
        self.assertEqual(self.reg.badge_number(), string_id)

    # TODO: More validation tests
    def test_registration_validation_extralongname(self):
        self.reg.clean_fields(exclude=['address', 'city', 'state', 'postal_code', 'country'])
        self.reg.save()
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.__str__(),
                         '{0} [{1}]'.format(self.reg.name, self.reg.badge_name))
        self.reg.badge_name = 'This is an unbelievably long badge name. Actually, it is kind of believable, given what people try to register as.'
        self.assertRaises(ValidationError, self.reg.clean_fields,
                          exclude=['address', 'city', 'state', 'postal_code', 'country'])



class RegistrationLevelModelTest(TestCase):
    def test_registration_level_model_str(self):
        convention = create_test_convention()
        levels = create_test_registrationlevels(convention)
        self.assertEqual(str(levels['sponsor']),
            '{0} [{1}]'.format(levels['sponsor'].title, levels['sponsor'].convention.name))

    def test_registration_level_current(self):
        convention = create_test_convention()
        future = create_test_convention('Future Convention', site_id=None)
        levels = create_test_registrationlevels(future, names=['basic', 'sponsor', 'supersponsor'])

        # Levels for a different convention won't appear
        self.assertEqual(models.RegistrationLevel.current.count(), 0)

        # But they will when that convention becomes the current one
        convention.site_id = None
        convention.save()
        future.site_id = settings.SITE_ID
        future.save()
        # By default all are active and available
        self.assertEqual(models.RegistrationLevel.current.count(), 3)

        # Conditions which exclude levels from appearing current
        levels['basic'].active = False
        levels['basic'].save()
        self.assertEqual(models.RegistrationLevel.current.count(), 2)
        levels['sponsor'].opens = timezone.now() + timedelta(days=1)
        levels['sponsor'].save()
        self.assertEqual(models.RegistrationLevel.current.count(), 1)
        levels['supersponsor'].deadline = timezone.now() - timedelta(days=1)
        levels['supersponsor'].save()
        self.assertEqual(models.RegistrationLevel.current.count(), 0)

        # They should still appear through the objects manager
        self.assertEqual(models.RegistrationLevel.objects.count(), 3)


class RegistrationUpgradeModelTest(TestCase):
    def test_upgrade_name(self):
        convention = create_test_convention()
        levels = create_test_registrationlevels(convention)

        upgrade = models.RegistrationUpgrade.objects.create(
            current_registration_level=levels['basic'],
            upgrade_registration_level=levels['sponsor'],
            description='',
            active=True
        )
        models.RegistrationUpgradePrice.objects.create(
            registration_upgrade = upgrade,
            price=levels['sponsor'].price - levels['basic'].price,
            active_date=timezone.now() - timedelta(days=365)
        )
        self.assertEqual(str(upgrade), 'basic to sponsor')

    def test_upgrade_deny_between_conventions(self):
        convention1 = create_test_convention('First Convention', site_id=None)
        levels1 = create_test_registrationlevels(convention1)
        convention2 = create_test_convention('Next Convention')
        levels2 = create_test_registrationlevels(convention2)

        upgrade = models.RegistrationUpgrade.objects.create(
            current_registration_level=levels1['basic'],
            upgrade_registration_level=levels2['sponsor'],
            description='',
            active=True
        )
        models.RegistrationUpgradePrice.objects.create(
            registration_upgrade = upgrade,
            price=levels2['sponsor'].price - levels1['basic'].price,
            active_date=timezone.now() - timedelta(days=365)
        )
        self.assertRaises(ValidationError, upgrade.full_clean)


class ShirtSizeModelTest(TestCase):
    def test_convention_model(self):
        size = 'small'
        shirt_size = create_test_shirtsizes(names=[size])[size]
        self.assertEqual(str(shirt_size), shirt_size.size)


# View Tests

class CheckInViewTest(TestCase):
    """On-site registration check-in pages test"""
    def setUp(self):
        convention = create_test_convention()
        create_test_paymentmethod('Cash')
        self.levels = create_test_registrationlevels(convention)
        swag = models.Swag.objects.create(convention=convention, description='Lanyard')
        self.levels['basic'].registrationlevelswag_set.create(swag=swag)
        swag = models.Swag.objects.create(convention=convention, description='Shirt')
        self.levels['sponsor'].registrationlevelswag_set.create(swag=swag)
        self.levels['supersponsor'].registrationlevelswag_set.create(swag=swag)
        swag = models.Swag.objects.create(convention=convention, description='Hoodie')
        self.levels['supersponsor'].registrationlevelswag_set.create(swag=swag)
        self.upgrades = create_test_registrationupgrades([
            (self.levels['basic'], self.levels['sponsor']),
            (self.levels['basic'], self.levels['supersponsor']),
            (self.levels['sponsor'], self.levels['supersponsor']),
        ])
        self.max_upgrade = self.upgrades[1]
        # Create a registration with unique name values
        self.reg = create_test_registration(self.levels['basic'],
            first_name='FName',
            last_name='LName',
            badge_name='BName',
            email='email@example.com',
        )
        # Second reg, since searches finding one now do a redirect
        self.reg2 = create_test_registration(self.levels['sponsor'],
            first_name='FName',
            last_name='LName',
            badge_name='BName',
            email='email@example.com',
        )

        # Create a registration lead user and group
        self.reglead = create_test_user(username='reglead', password='staff')
        self.reglead.is_staff = True
        self.reglead.save()
        reglead_group = Group.objects.create(name='reglead')
        self.reglead.groups.set([reglead_group])

        # Create a rank-and-file registration user and group
        self.regraf = create_test_user(username='regraf', password='staff')
        self.regraf.save()
        reg_group = Group.objects.create(name='regraf')
        self.regraf.groups.set([reg_group])

    def test_base(self):
        # Check-in requires a staff account
        # Fail without login
        response = self.client.get(reverse('convention_check_in'))
        self.assertEqual(response.status_code, 302)

        # Fail when logged in as non-staff
        create_test_user(username='user', password='user')
        self.client.login(username='user', password='user')
        response = self.client.get(reverse('convention_check_in'))
        self.assertEqual(response.status_code, 302)

        # Fail when logged in as non-registration staff
        user = create_test_user(username='otherstaff', password='otherstaff')
        user.is_staff = True
        user.save()
        self.client.login(username='otherstaff', password='otherstaff')
        response = self.client.get(reverse('convention_check_in'))
        self.assertEqual(response.status_code, 302)

        # Succeed when logged in as an old school registration group staff
        user = create_test_user(username='staff', password='staff')
        user.is_staff = True
        user.groups.set([Group.objects.create(name='registration')])
        user.save()
        self.client.login(username='staff', password='staff')
        response = self.client.get(reverse('convention_check_in'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Search' in response.content)

        # Fail when logged in as rank-and-file reg staff
        self.client.login(username='regraf', password='staff')
        response = self.client.get(reverse('convention_check_in'))
        self.assertEqual(response.status_code, 302)
        # .. Unless the terminal is authorized by a reg lead signing in first
        self.client.login(username='reglead', password='staff')
        response = self.client.get(reverse('convention_check_in'))
        # .. Then a rank-and-file reg staffer will be authorized
        self.client.login(username='regraf', password='staff')
        response = self.client.get(reverse('convention_check_in'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Search' in response.content)

        # Succeed when logged in as a registration lead
        self.client.login(username='reglead', password='staff')
        response = self.client.get(reverse('convention_check_in'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Search' in response.content)

    def test_search(self):
        # Authorize terminal, run as rank-and-file
        self.client.login(username='reglead', password='staff')
        response = self.client.get(reverse('convention_check_in'))
        self.client.login(username='regraf', password='staff')

        # Search by single name, all of which appear in the results table
        for name in [b'FName', b'LName', b'BName']:
            response = self.client.post(reverse('convention_check_in'), {'search': str(name)})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(name in response.content)

        # Complex, multi-parameter, case-insensitive searches
        response = self.client.post(reverse('convention_check_in'), {'search': 'FNAME lname'})
        self.assertTrue(b'BName' in response.content)
        response = self.client.post(reverse('convention_check_in'), {'search': 'LN bname'})
        self.assertTrue(b'FName' in response.content)
        response = self.client.post(reverse('convention_check_in'), {'search': 'fn email'})
        self.assertTrue(b'LName' in response.content)

        # Finding a single registration will redirect to that check-in page
        response = self.client.post(reverse('convention_check_in'), {'search': self.reg.external_id})
        self.assertEqual(response.status_code, 302)

        # Similarly searching by badge numbers hould find the badge
        response = self.client.post(reverse('convention_check_in'), {'search': '00{}'.format(self.reg.id)})
        self.assertEqual(response.status_code, 302)

        # Find nothing, shows no table at all
        response = self.client.post(reverse('convention_check_in'), {'search': 'xyz'})
        self.assertTrue(b'Paid' not in response.content)

    def test_swipe_search(self):
        # Authorize terminal, run as rank-and-file
        self.client.login(username='reglead', password='staff')
        response = self.client.get(reverse('convention_check_in'))
        self.client.login(username='regraf', password='staff')
        self.reg.status = 1
        self.reg.save()

        # Test exact match
        birthday = (timezone.now() - timedelta(days=18*366)).date()
        response = self.client.post(reverse('convention_check_in'), {'c_last': 'LName',
                                                                     'c_first': 'FName',
                                                                     'c_birthday': birthday.isoformat()})
        # Our test case still matches 2 registrations, so we get a list...
        self.assertEqual(response.status_code, 200)
        # ... But now the values are stored in the session
        for key in ['c_last', 'c_first', 'c_birthday']:
            self.assertTrue(key in self.client.session)

        # And if we bring up the check-in page for one of them
        response = self.client.get(reverse('convention_check_in', args=[self.reg.id]))
        self.assertEqual(response.status_code, 200)
        # The system should detect that the names and birthday match exactly
        self.assertTrue(b'"confirm_name" checked' in response.content)
        self.assertTrue(b'"confirm_birthday" checked' in response.content)

        # Maybe the registered first name was a shortened version?
        response = self.client.post(reverse('convention_check_in'), {'c_last': 'LNAME',
                                                                     'c_first': 'FNAMEFULL',
                                                                     'c_birthday': birthday.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'FName' in response.content)
        # Name shouldn't show an exact match on the check-in page
        response = self.client.get(reverse('convention_check_in', args=[self.reg.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(b'"confirm_name" checked' in response.content)
        self.assertTrue(b'"confirm_birthday" checked' in response.content)

        # Wrong birthday
        birthday = (timezone.now() - timedelta(days=17*366)).date()
        response = self.client.post(reverse('convention_check_in'), {'c_last': 'LNAME',
                                                                     'c_first': 'FNAME',
                                                                     'c_birthday': birthday.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'FName' in response.content)
        # Birthday shouldn't show an exact match on the check-in page
        response = self.client.get(reverse('convention_check_in', args=[self.reg.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'"confirm_name" checked' in response.content)
        self.assertFalse(b'"confirm_birthday" checked' in response.content)

        # Both technically wrong, but still findable
        birthday = (timezone.now() - timedelta(days=17*366)).date()
        response = self.client.post(reverse('convention_check_in'), {'c_last': 'LNAME',
                                                                     'c_first': 'FNAMEFULL',
                                                                     'c_birthday': birthday.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'FName' in response.content)
        # Birthday shouldn't show an exact match on the check-in page
        response = self.client.get(reverse('convention_check_in', args=[self.reg.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(b'"confirm_name" checked' in response.content)
        self.assertFalse(b'"confirm_birthday" checked' in response.content)

        # Married, or completely different
        response = self.client.post(reverse('convention_check_in'), {'c_last': 'DIFFERENTNAME',
                                                                     'c_first': 'xyz',
                                                                     'c_birthday': birthday.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Paid' not in response.content)

        # A general search should then clear the session variables
        response = self.client.post(reverse('convention_check_in'), {'search': ''})
        for key in ['c_last', 'c_first', 'c_birthday']:
            self.assertFalse(key in self.client.session)

    def test_check_in(self):
        # Authorize terminal, run as rank-and-file
        self.client.login(username='reglead', password='staff')
        response = self.client.get(reverse('convention_check_in'))
        self.client.login(username='regraf', password='staff')

        # Our test registration is not paid by default
        response = self.client.get(reverse('convention_check_in', args=[self.reg.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'>UNPAID<' in response.content)
        self.assertTrue(b'disabled>Print Badge' in response.content)
        self.assertTrue(b'disabled>Upgrade' in response.content)
        self.assertTrue(b'Take payment for registration' in response.content)

        # Take Payment... Page should reflect new status
        response = self.client.post(reverse('convention_check_in', args=[self.reg.id]),
                                    {'registration_level': self.reg.registration_level.id})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'>PAID<' in response.content)
        self.assertTrue(b'">Print Badge' in response.content)
        self.assertTrue(b'">Upgrade' in response.content)
        self.assertTrue(b'name="set_check_in" value="1"' in response.content)
        # And the registration object should be updated
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.status, 1)
        self.assertTrue(self.reg.payment_set.count() == 1)

        # Perform check-in
        response = self.client.post(reverse('convention_check_in', args=[self.reg.id]),
                                    {'set_check_in': "1"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'>CHECKED IN<' in response.content)
        self.assertTrue(b'">Print Badge' in response.content)
        self.assertTrue(b'name="set_check_in" value="0"' in response.content)
        # And again the registration object should reflect that
        self.reg.refresh_from_db()
        self.assertTrue(self.reg.checked_in)

    def test_private_check_in(self):
        # Authorize terminal, run as rank-and-file
        self.client.login(username='reglead', password='staff')
        response = self.client.get(reverse('convention_check_in'))
        self.client.login(username='regraf', password='staff')

        # The reg staffer encounters a reg that's marked private
        self.reg.status = 1
        self.reg.private_check_in = True
        self.reg.save()
        response = self.client.get(reverse('convention_check_in', args=[self.reg.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'registration supervisor on duty' in response.content)
        self.assertFalse(b'name="set_check_in" value="1"' in response.content)
        # And if they somehow try, they should be prevented
        response = self.client.post(reverse('convention_check_in', args=[self.reg.id]),
                                    {'set_check_in': "1"})
        self.assertFalse(b'>CHECKED IN<' in response.content)
        self.reg.refresh_from_db()
        self.assertFalse(self.reg.checked_in)

        # The registration lead logs in, and is able to do the check-in
        self.client.login(username='reglead', password='staff')
        response = self.client.get(reverse('convention_check_in', args=[self.reg.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'registration requires a registration supervisor' in response.content)
        self.assertTrue(b'name="set_check_in" value="1"' in response.content)
        response = self.client.post(reverse('convention_check_in', args=[self.reg.id]),
                                    {'set_check_in': "1"})
        self.assertTrue(b'>CHECKED IN<' in response.content)
        self.reg.refresh_from_db()
        self.assertTrue(self.reg.checked_in)

        # Perform check-in
        response = self.client.post(reverse('convention_check_in', args=[self.reg.id]),
                                    {'set_check_in': "1"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'>CHECKED IN<' in response.content)
        self.assertTrue(b'">Print Badge' in response.content)
        self.assertTrue(b'name="set_check_in" value="0"' in response.content)
        # And again the registration object should reflect that
        self.reg.refresh_from_db()
        self.assertTrue(self.reg.checked_in)

    def test_check_in_pay_new_level(self):
        """Support the case where someone fills in an on-site cash reg
            then wants to immediately upgrade and pay for a different
            level at check-in time."""
        # Authorize terminal, run as rank-and-file
        self.client.login(username='reglead', password='staff')
        response = self.client.get(reverse('convention_check_in'))
        self.client.login(username='regraf', password='staff')

        # Our test registration is not paid by default
        response = self.client.get(reverse('convention_check_in', args=[self.reg.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'<strong>basic</strong>' in response.content)

        response = self.client.post(reverse('convention_check_in', args=[self.reg.id]),
                                    {'registration_level': self.levels['sponsor'].id})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'<strong>sponsor</strong>' in response.content)

    def test_check_in_undo(self):
        # Authorize terminal, run as rank-and-file
        self.client.login(username='reglead', password='staff')
        response = self.client.get(reverse('convention_check_in'))
        self.client.login(username='regraf', password='staff')
        self.reg.status = 1
        self.reg.checked_in = True
        self.reg.save()

        # Post the undo check-in flag
        response = self.client.post(reverse('convention_check_in', args=[self.reg.id]),
                                    {'set_check_in': "0"})
        self.assertEqual(response.status_code, 200)
        # The registration should now reflect that
        self.assertTrue(b'>PAID<' in response.content)
        self.assertTrue(b'name="set_check_in" value="1"' in response.content)
        self.reg.refresh_from_db()
        self.assertFalse(self.reg.checked_in)

    def test_received_swag(self):
        # Authorize terminal, run as rank-and-file
        self.client.login(username='reglead', password='staff')
        response = self.client.get(reverse('convention_check_in'))
        self.client.login(username='regraf', password='staff')
        self.reg.status = 1
        self.reg.checked_in = True
        self.reg.registration_level = self.levels['sponsor']
        self.reg.save()
        self.assertEqual(len(self.reg.registrationswag_set.all()), 0)

        response = self.client.get(reverse('convention_check_in', args=[self.reg.id]))
        self.assertEqual(len(response.context['received_swag']), 0)
        self.assertEqual(len(response.context['owed_swag']), 1)
        # Post the received swag flag
        response = self.client.post(reverse('convention_check_in', args=[self.reg.id]),
            {
                'set_received_swag': '1',
                'new_received_{}'.format(self.levels['sponsor'].registrationlevelswag_set.first().swag.id): '1'
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['received_swag']), 1)
        self.assertEqual(len(response.context['owed_swag']), 0)
        # The registration should now reflect that
        self.reg.refresh_from_db()
        self.assertEqual(len(self.reg.registrationswag_set.all()), 1)

        # Registration is upgraded
        self.reg.registration_level = self.levels['supersponsor']
        self.reg.save()
        response = self.client.get(reverse('convention_check_in', args=[self.reg.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['received_swag']), 1)
        self.assertEqual(len(response.context['owed_swag']), 1)
        response = self.client.post(reverse('convention_check_in', args=[self.reg.id]),
            {
                'set_received_swag': '1',
                'backordered_{}'.format(self.reg.registrationswag_set.first().id): '1',
                'new_received_{}'.format(response.context['owed_swag'][0].id): '1'
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['received_swag']), 2)
        self.assertEqual(len(response.context['owed_swag']), 0)
        # The registration should now reflect one received, one backordered
        self.reg.refresh_from_db()
        self.assertEqual(len(self.reg.registrationswag_set.filter(received=True)), 1)
        self.assertEqual(len(self.reg.registrationswag_set.filter(backordered=True)), 1)

        # Uncheck everything, similar to old undo
        response = self.client.post(reverse('convention_check_in', args=[self.reg.id]),
                                    {'set_received_swag': '1'})
        self.assertEqual(response.status_code, 200)
        self.reg.refresh_from_db()
        self.assertEqual(len(self.reg.registrationswag_set.all()), 0)

    def test_edit(self):
        # Authorize terminal, run as rank-and-file
        self.client.login(username='reglead', password='staff')
        response = self.client.get(reverse('convention_check_in'))
        self.client.login(username='regraf', password='staff')

        # Editing a registration should show a form
        response = self.client.get(reverse('convention_check_in', args=[self.reg.id, 'edit']))
        self.assertEqual(response.status_code, 200)
        self.assertTrue('Edit {}'.format(self.reg.badge_name).encode('utf-8') in response.content)

        # TODO: Test POST

    def test_print(self):
        # Authorize terminal, run as rank-and-file
        self.client.login(username='reglead', password='staff')
        response = self.client.get(reverse('convention_check_in'))
        self.client.login(username='regraf', password='staff')

        # Unpaid registrations cannot be printed
        response = self.client.get(reverse('convention_check_in', args=[self.reg.id, 'print']))
        self.assertEqual(response.status_code, 403)

        # After payment is received, printing resets the needs_print flag
        self.reg.status = 1
        self.reg.needs_print = 1
        self.reg.save()

        response = self.client.get(reverse('convention_check_in', args=[self.reg.id, 'print']))
        self.assertEqual(response.status_code, 200)
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.needs_print, 0)

        # And the check-in page should indicate it's a re-print
        response = self.client.get(reverse('convention_check_in', args=[self.reg.id]))
        self.assertTrue(b'Re-print Badge' in response.content)

    def test_upgrade(self):
        # Authorize terminal, run as rank-and-file
        self.client.login(username='reglead', password='staff')
        response = self.client.get(reverse('convention_check_in'))
        self.client.login(username='regraf', password='staff')
        self.reg.status = 1
        self.reg.needs_print = 0
        self.reg.save()

        # Show upgrade options
        response = self.client.get(reverse('convention_check_in', args=[self.reg.id, 'upgrade']))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'<strong>basic</strong>' in response.content)
        self.assertTrue(b'sponsor' in response.content)

        # Upgrade from basic to supersponsor, redirect back to check-in page
        response = self.client.post(reverse('convention_check_in', args=[self.reg.id, 'upgrade']),
                                            {'registration_level': self.max_upgrade.id})
        self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse('convention_check_in', args=[self.reg.id]))
        # Can't upgrade past that...
        self.assertTrue(b'disabled>Upgrade' in response.content)
        # And tell staff to reprint badge
        self.assertTrue(b'Discard old badge and print a new' in response.content)


class ConfirmViewTest(TestCase):
    """Registration confirmation page test"""
    def setUp(self):
        convention = create_test_convention()
        self.levels = create_test_registrationlevels(convention)
        self.reg = create_test_registration(self.levels['basic'])
        self.reg.status = 1
        self.reg.save()

    def test_upgradeable(self):
        upgrade = create_test_registrationupgrades([
            (self.levels['basic'], self.levels['sponsor'])
        ])[0]

        cache.clear()
        response = self.client.get(reverse('convention_confirm', args=[self.reg.external_id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Upgrade My Registration' in response.content)

        # Set registration to the highest level
        self.reg.registration_level = self.levels['supersponsor']
        self.reg.save()

        cache.clear()
        response = self.client.get(reverse('convention_confirm', args=[self.reg.external_id]))
        self.assertEqual(response.status_code, 200)
        # No longer able to upgrade
        self.assertTrue(b'No Upgrade Available' in response.content)

    def test_self_updateable(self):
        cache.clear()
        response = self.client.get(reverse('convention_confirm', args=[self.reg.external_id]))
        # A new badge can be changed
        self.assertTrue(b'Change Badge Name or Avatar' in response.content)

        # Unless the badge has already been printed
        self.reg.needs_print = 0
        self.reg.save()
        cache.clear()
        response = self.client.get(reverse('convention_confirm', args=[self.reg.external_id]))
        self.assertFalse(b'Change Badge Name or Avatar' in response.content)
        self.assertTrue(b'Your badge has already been printed' in response.content)

    def test_claimable(self):
        # Clear user account...
        self.reg.user = None
        self.reg.save()

        cache.clear()
        response = self.client.get(reverse('convention_confirm', args=[self.reg.external_id]))
        # A badge not associated with a user can be claimed, once logged in...
        self.assertFalse(b'Claim This Registration' in response.content)

        user = create_test_user(username='user', password='user')
        self.client.login(username='user', password='user')

        cache.clear()
        response = self.client.get(reverse('convention_confirm', args=[self.reg.external_id]))
        self.assertTrue(b'Claim This Registration' in response.content)

        # Unless the reg is indeed associated with a user
        self.reg.user = user
        self.reg.save()
        cache.clear()
        response = self.client.get(reverse('convention_confirm', args=[self.reg.external_id]))
        self.assertFalse(b'Claim This Registration' in response.content)

    def test_failure_rate_limit(self):
        response = self.client.get(reverse('convention_confirm', args=[self.reg.external_id]))
        self.assertEqual(response.status_code, 200)

        cache.clear()
        # Different external_id should not exist
        for val in range(0, 6):
            response = self.client.get(reverse('convention_confirm', args=[self.reg.external_id + str(val)]))
            self.assertEqual(response.status_code, 404)

        # Subsequent access to existing ID should still 404
        response = self.client.get(reverse('convention_confirm', args=[self.reg.external_id]))
        self.assertEqual(response.status_code, 404)


class UserRegChangeViewTest(TestCase):
    """Registration self-update process views test"""
    def setUp(self):
        convention = create_test_convention()
        self.levels = create_test_registrationlevels(convention)
        self.reg = create_test_registration(self.levels['basic'])
        self.reg.status = 1
        self.reg.save()

    def test_change_form(self):
        response = self.client.get(reverse('convention_confirm_change', args=[self.reg.external_id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'You can update the following information' in response.content)

    def test_change_badge_name(self):
        old_badge_name = self.reg.badge_name
        new_badge_name = 'Finck Rat'
        response = self.client.post(reverse('convention_confirm_change', args=[self.reg.external_id]),
                                    {'new_badge_name': new_badge_name})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Registration Update Confirmation' in response.content)
        self.assertEqual(len(mail.outbox), 1)

        # Extract the confirmation URL from the email message
        self.assertIn('/confirm/{0}'.format(self.reg.external_id), mail.outbox[0].body)
        change_url = re.search('/confirm/{0}.*'.format(self.reg.external_id), mail.outbox[0].body).group()

        # Before the confirmation we should still have the old badge name
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.badge_name, old_badge_name)

        # Confirm the change
        response = self.client.get(change_url)
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.badge_name, new_badge_name)

    # TODO: test_change_avatar

    def test_change_nothing(self):
        new_badge_name = self.reg.badge_name
        # If we POST a request to change the badge to the same thing we should just redirect
        response = self.client.post(reverse('convention_confirm_change', args=[self.reg.external_id]),
                                    {'new_badge_name': new_badge_name})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)


class UserRegClaimViewTest(TestCase):
    """Registration user association claim test"""
    def setUp(self):
        convention = create_test_convention()
        self.levels = create_test_registrationlevels(convention)
        self.reg = create_test_registration(self.levels['basic'])
        self.reg.status = 1
        self.default_user = self.reg.user
        self.reg.user = None
        self.reg.save()

    def test_claim_registration(self):
        user = create_test_user(username='user', password='user')
        self.client.login(username='user', password='user')

        self.assertIsNone(self.reg.user)
        # Logged in user clicks the claim link
        response = self.client.get(reverse('convention_confirm_claim', args=[self.reg.external_id]))
        self.reg.refresh_from_db()
        self.assertIsNotNone(self.reg.user)

    def test_claim_already_associated(self):
        user = create_test_user(username='user', password='user')
        self.client.login(username='user', password='user')

        # Unless the reg is indeed associated with a user
        self.reg.user = self.default_user
        self.reg.save()
        response = self.client.get(reverse('convention_confirm_claim', args=[self.reg.external_id]))
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.user, self.default_user)
        self.assertNotEqual(self.reg.user, user)

    def test_claim_not_logged_in(self):
        response = self.client.get(reverse('convention_confirm_claim', args=[self.reg.external_id]))
        self.reg.refresh_from_db()
        self.assertIsNone(self.reg.user)


class QRCodeViewTest(TestCase):
    """Badge ID QR code image test"""
    def setUp(self):
        convention = create_test_convention()
        convention.registrationsettings.badge_offset = 0
        convention.save()
        self.levels = create_test_registrationlevels(convention)
        self.reg = create_test_registration(self.levels['basic'],
            first_name='FName',
            last_name='LName',
            badge_name='BName',
            email='email@example.com',
        )
        self.reg.status = 1
        self.reg.needs_print = 0
        self.reg.save()
        self.user = create_test_user(username='staff', password='staff')
        self.user.is_staff = True
        self.user.save()

    def test_base(self):
        # QR code display only for badge printing use, thus requires a staff account
        # Fail without login
        response = self.client.get(reverse('badge_qrcode', kwargs={'badge_number': self.reg.badge_number()}))
        self.assertEqual(response.status_code, 302)

        # Fail when logged in as non-staff
        create_test_user(username='user', password='user')
        self.client.login(username='user', password='user')
        response = self.client.get(reverse('badge_qrcode', kwargs={'badge_number': self.reg.badge_number()}))
        self.assertEqual(response.status_code, 302)

        # Succeed when logged in as staff
        self.client.login(username='staff', password='staff')
        response = self.client.get(reverse('badge_qrcode', kwargs={'badge_number': self.reg.badge_number()}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')

class RegisterViewTest(TestCase):
    def setUp(self):
        self.convention = create_test_convention()
        self.levels = create_test_registrationlevels(self.convention)
        self.payment_method = create_test_paymentmethod('Cash')

        example_birthday=(timezone.now() - timedelta(days=18*366)).date()
        self.example_reg = dict(
            first_name='Drykath',
            last_name='Dragon',
            badge_name='Drykath',
            email='drykath@example.com',
            address='21111 Haggerty Rd',
            city='Novi',
            state='MI',
            postal_code='48375',
            country='United States',
            registration_level = self.levels['sponsor'].id,
            birthday_month=example_birthday.month,
            birthday_day=example_birthday.day,
            birthday_year=example_birthday.year,
            payment_method=self.payment_method.id,
            shirt_size=create_test_shirtsizes()['small'].id,
            emergency_contact='',
            volunteer_phone='',
            coupon_code='',
            tos='on',
        )

        self.coupon = models.CouponCode.objects.create(
            convention=self.convention,
            code='one-dollar',
            discount=1,
            percent=False,
        )
        self.full_coupon = models.CouponCode.objects.create(
            convention=self.convention,
            code='full-discount',
            discount=100,
            percent=True,
        )

#    def test_unauthenticated(self):
#        response = self.client.get(reverse('convention_registration'))
#        self.assertEqual(response.status_code, 200)
#        self.assertTrue(b'check back on your registrations later' in response.content)

    def test_authenticated_get(self):
        user = create_test_user(username='drykath', password='drykath')
        response = self.client.login(username='drykath', password='drykath')
        self.client.session['avatar'] = 0
        self.client.session.save()
        self.assertEqual(response, True)
        response = self.client.get(reverse('convention_registration'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'check back on your registrations later' not in response.content)

    def test_preselected_level(self):
        response = self.client.get(reverse('convention_registration'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('registration_level', response.context['form'].initial)
        response = self.client.get(reverse('convention_registration'), {'as': 'basic'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('registration_level', response.context['form'].initial)
        self.assertEqual(response.context['form'].initial['registration_level'], str(self.levels['basic'].id))

    def test_prefilled_coupon_code(self):
        generated_html = ' name="coupon_code" value="xyz"'.encode('utf-8')
        response = self.client.get(reverse('convention_registration'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(generated_html not in response.content)
        response = self.client.get(reverse('convention_registration'), {'coupon_code': 'xyz'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(generated_html in response.content)

    def test_post_step1_invalid(self):
        response = self.client.post(reverse('convention_registration'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Please verify the following information is correct' not in response.content)

        # Must be 18 years of age to register
        too_young = self.example_reg
        too_young['birthday_year'] += 2
        response = self.client.post(reverse('convention_registration'), too_young)
        self.assertTrue(b'Please verify the following information is correct' not in response.content)
        self.assertTrue(b'You must be 18 or older to register' in response.content)

        # If a coupon code is provided it must be a valid one...
        invalid_coupon = self.example_reg
        invalid_coupon['coupon_code'] = 'invalid'
        response = self.client.post(reverse('convention_registration'), too_young)
        self.assertTrue(b'Please verify the following information is correct' not in response.content)
        self.assertTrue(b'That coupon code is not valid' in response.content)

        # ... that hasn't been used before
        self.full_coupon.single_use = True
        self.full_coupon.save()
        other_registration = create_test_registration(self.levels['basic'])
        models.CouponUse.objects.create(
            registration=other_registration,
            coupon=self.full_coupon
        )
        invalid_coupon = self.example_reg
        invalid_coupon['coupon_code'] = 'full-discount'
        response = self.client.post(reverse('convention_registration'), too_young)
        self.assertTrue(b'Please verify the following information is correct' not in response.content)
        self.assertTrue(b'That coupon code has already been used' in response.content)

        # ... from this year/convention
        self.full_coupon.convention = create_test_convention('Last Year', site_id=None)
        self.full_coupon.save()
        invalid_coupon = self.example_reg
        invalid_coupon['coupon_code'] = 'full-discount'
        response = self.client.post(reverse('convention_registration'), too_young)
        self.assertTrue(b'Please verify the following information is correct' not in response.content)
        self.assertTrue(b'That coupon code is not valid' in response.content)

        # Must accept TOS to register
        accept_tos = self.example_reg
        del accept_tos['tos']
        response = self.client.post(reverse('convention_registration'), accept_tos)
        self.assertTrue(b'This field is required' in response.content)

    def test_post_step1_limited(self):
        self.levels['sponsor'].limit = 1
        self.levels['sponsor'].save()
        create_test_registration(self.levels['sponsor'], status=1)

        response = self.client.post(reverse('convention_registration'), self.example_reg)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(b'Please verify the following information is correct' in response.content)
        self.assertTrue(b'That registration level is no longer available' in response.content)

    def test_post_step1_valid(self):
        response = self.client.post(reverse('convention_registration'), self.example_reg)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Please verify the following information is correct' in response.content)

        # one-dollar coupon discount
        self.example_reg['coupon_code'] = self.coupon.code
        response = self.client.post(reverse('convention_registration'), self.example_reg)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['amount'], self.levels['sponsor'].price - self.coupon.discount)

        # Full coupon discount
        self.example_reg['coupon_code'] = self.full_coupon.code
        response = self.client.post(reverse('convention_registration'), self.example_reg)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['amount'], 0)

    def test_post_step2_cash(self):
        # Usual process
        self.assertEqual(models.Registration.all_registrations.count(), 0)

        response = self.client.post(reverse('convention_registration'), self.example_reg)
        response = self.client.post(reverse('convention_registration'), {'confirm': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Successfully registered!' in response.content)
        self.assertEqual(len(mail.outbox), 1)

        # Should create a registration...
        self.assertEqual(models.Registration.all_registrations.count(), 1)
        reg = models.Registration.all_registrations.first()
        # ... that is not paid and has no payment record
        self.assertEqual(reg.status, 0)
        self.assertEqual(reg.payment_set.count(), 0)

    def test_post_step2_coupon(self):
        # Zero value registration due to coupon code
        self.assertEqual(models.Registration.objects.count(), 0)

        self.example_reg['coupon_code'] = self.full_coupon.code
        response = self.client.post(reverse('convention_registration'), self.example_reg)
        response = self.client.post(reverse('convention_registration'), {'confirm': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Successfully registered!' in response.content)
        self.assertEqual(len(mail.outbox), 1)

        # Should create a registration...
        self.assertEqual(models.Registration.objects.count(), 1)
        reg = models.Registration.objects.first()
        # ... that is paid and has a coupon linked
        self.assertEqual(reg.status, 1)
        self.assertEqual(reg.couponuse_set.count(), 1)

    def test_post_step2_coupon_force_level(self):
        self.full_coupon.force_registration_level = self.levels['basic']
        self.full_coupon.save()

        # User tries to register as sponsor, but using a forced basic coupon
        self.example_reg['registration_level'] = self.levels['sponsor'].id
        self.example_reg['coupon_code'] = self.full_coupon.code
        response = self.client.post(reverse('convention_registration'), self.example_reg)
        response = self.client.post(reverse('convention_registration'), {'confirm': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Successfully registered!' in response.content)

        # Should create a registration...
        self.assertEqual(models.Registration.objects.count(), 1)
        reg = models.Registration.objects.first()
        # ... that is a basic registration
        self.assertEqual(reg.registration_level, self.levels['basic'])

    def test_post_step2_coupon_force_dealer(self):
        # Zero value registration due to coupon code
        self.assertEqual(models.Registration.objects.count(), 0)

        dealer_registration_level = models.DealerRegistrationLevel.objects.create(
            convention=self.convention,
            number_tables=1,
            price=1.00,
        )
        self.full_coupon.force_dealer_registration_level = dealer_registration_level
        self.full_coupon.save()

        self.example_reg['coupon_code'] = self.full_coupon.code
        response = self.client.post(reverse('convention_registration'), self.example_reg)
        response = self.client.post(reverse('convention_registration'), {'confirm': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Successfully registered!' in response.content)

        # Should create a registration...
        self.assertEqual(models.Registration.objects.count(), 1)
        reg = models.Registration.objects.first()
        # ... that has a dealer table assigned
        self.assertEqual(reg.dealer_registration_level, dealer_registration_level)

#    @mock.patch('stripe.Charge.create')
#    def test_post_step2_credit(self, create_mock):
#        self.payment_method.is_credit = True
#        self.payment_method.save()
#
#        class charge(object):
#            id = 'ch_1234'
#
#        create_mock.return_value = charge()
#
#        response = self.client.post(reverse('convention_registration'), self.example_reg)
#        response = self.client.post(reverse('convention_registration'), {'confirm': '1', 'stripeToken': 'foo'})
#        self.assertEqual(response.status_code, 200)
#        self.assertTrue(b'Successfully registered!' in response.content)
#        self.assertEqual(len(mail.outbox), 1)
#
#        # Should create a paid registration...
#        self.assertEqual(models.Registration.objects.count(), 1)
#        reg = models.Registration.objects.first()
#        self.assertEqual(reg.status, 1)
#        self.assertEqual(reg.payment_set.count(), 1)

    def test_post_avatar(self):
        # Usual process
        self.assertEqual(models.Registration.all_registrations.count(), 0)

        # Inject a file upload into the POST, just a basic image
        im = Image.new('RGBA', (400,400))
        im_output = BytesIO()
        im.save(im_output, format='png')
        im_output.seek(0)
        self.example_reg['avatar'] = im_output

        response = self.client.post(reverse('convention_registration'), self.example_reg)
        self.assertTrue(b'Badge Avatar' in response.content)
        response = self.client.post(reverse('convention_registration'), {'confirm': '1'})
        self.assertEqual(response.status_code, 200)
        reg = models.Registration.all_registrations.first()
        self.assertTrue(reg.avatar)
        self.assertTrue(str(reg.id) in reg.avatar.path)

    def test_convention_closed(self):
        self.convention.registrationsettings.registration_open = False
        self.convention.registrationsettings.save()
        response = self.client.get(reverse('convention_registration'))
        self.assertEqual(response.status_code, 403)
        response = self.client.post(reverse('convention_registration'), {})
        self.assertEqual(response.status_code, 403)


class DealerUpgradeViewTest(TestCase):
    def setUp(self):
        self.convention = create_test_convention()
        self.levels = create_test_registrationlevels(self.convention)
        self.upgrades = create_test_registrationupgrades([
            (self.levels['basic'], self.levels['sponsor']),
            (self.levels['basic'], self.levels['supersponsor']),
            (self.levels['sponsor'], self.levels['supersponsor']),
        ])
        self.dealer_level = models.DealerRegistrationLevel.objects.create(
            convention=self.convention,
            title='Single',
            number_tables=1,
            price=5,
        )
        self.payment_method = create_test_paymentmethod('Credit', True)
        self.reg = create_test_registration(self.levels['sponsor'])
        self.reg.status = 1
        self.reg.save()

        self.regular_coupon = models.CouponCode.objects.create(
            convention=self.convention,
            code='one-dollar',
            discount=1,
            percent=False,
        )
        self.dealer_coupon = models.CouponCode.objects.create(
            convention=self.convention,
            code='dealer',
            discount=0,
            force_dealer_registration_level=self.dealer_level,
        )

    def test_unauthenticated_noid(self):
        response = self.client.get(reverse('dealer_upgrade'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Confirmation code for dealer' in response.content)
        self.assertFalse(b'Select a registration to upgrade' in response.content)

    def test_unauthenticated_id(self):
        response = self.client.get(reverse('dealer_upgrade', kwargs={'external_id': self.reg.external_id}))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Coupon code' in response.content)

    def test_authenticated_single(self):
        user = create_test_user(username='drykath', password='drykath')
        self.reg.user = user
        self.reg.save()
        response = self.client.login(username='drykath', password='drykath')
        self.assertEqual(response, True)

        response = self.client.get(reverse('dealer_upgrade'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Coupon code' in response.content)

    def test_authenticated_multiple(self):
        user = create_test_user(username='drykath', password='drykath')
        self.reg.user = user
        self.reg.save()
        second_reg = create_test_registration(self.levels['sponsor'])
        second_reg.status = 1
        second_reg.user = user
        second_reg.save()
        response = self.client.login(username='drykath', password='drykath')
        self.assertEqual(response, True)

        response = self.client.get(reverse('dealer_upgrade'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Confirmation code for dealer' in response.content)
        self.assertTrue(b'Or select a registration to apply dealer table' in response.content)

        response = self.client.post(reverse('dealer_upgrade'), {'registration': self.reg.id})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Coupon code' in response.content)

    def test_authenticated_ineligible(self):
        user = create_test_user(username='drykath', password='drykath')
        self.reg.dealer_registration_level = self.dealer_level
        self.reg.user = user
        self.reg.save()
        response = self.client.login(username='drykath', password='drykath')
        self.assertEqual(response, True)

        response = self.client.get(reverse('dealer_upgrade', kwargs={'external_id': self.reg.external_id}))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'This registration already has dealer tables assigned' in response.content)

    def test_post_step1_blank(self):
        # Need to re-accept TOS
        data = {
            'registration': self.reg.id,
            'coupon_code': '',
            'payment_method': self.payment_method.id,
            'tos': 'on',
        }
        response = self.client.post(reverse('dealer_upgrade', kwargs={'external_id': self.reg.external_id}), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'This field is required' in response.content)

    def test_post_step1_invalid(self):
        # Need to re-accept TOS
        data = {
            'registration': self.reg.id,
            'coupon_code': self.regular_coupon.code,
            'payment_method': self.payment_method.id,
            'tos': 'on',
        }
        response = self.client.post(reverse('dealer_upgrade', kwargs={'external_id': self.reg.external_id}), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'This is not a dealer code' in response.content)

    def test_step1_valid(self):
        data = {
            'registration': self.reg.id,
            'coupon_code': self.dealer_coupon.code,
            'payment_method': self.payment_method.id,
            'tos': 'on',
        }
        response = self.client.post(reverse('dealer_upgrade', kwargs={'external_id': self.reg.external_id}), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Please verify the following information is correct' in response.content)
        self.assertEqual(response.context['amount'], self.dealer_level.price)

#    @mock.patch('stripe.Charge.create')
#    def test_step2_credit(self, create_mock):
#        self.payment_method.is_credit = True
#        self.payment_method.save()
#
#        class charge(object):
#            id = 'ch_1234'
#
#        create_mock.return_value = charge()
#
#        data = {
#            'registration': self.reg.id,
#            'coupon_code': self.dealer_coupon.code,
#            'payment_method': self.payment_method.id,
#            'stripeToken': 'foo',
#            'tos': 'on',
#        }
#        # Hit step 1
#        response = self.client.post(reverse('dealer_upgrade', kwargs={'external_id': self.reg.external_id}), data)
#        # Confirm in step 2
#        data['confirm'] = 1
#        response = self.client.post(reverse('dealer_upgrade', kwargs={'external_id': self.reg.external_id}), data)
#        self.assertEqual(response.status_code, 200)
#        self.assertTrue(b'Successfully upgraded!' in response.content)
#        self.assertEqual(len(mail.outbox), 1)
#
#        # Should upgrade the registration
#        self.reg.refresh_from_db()
#        self.assertEqual(self.reg.dealer_registration_level, self.dealer_level)

    def test_convention_closed(self):
        self.convention.registrationsettings.registration_open = False
        self.convention.registrationsettings.save()
        response = self.client.get(reverse('dealer_upgrade', kwargs={'external_id': self.reg.external_id}))
        self.assertEqual(response.status_code, 403)
        response = self.client.post(reverse('dealer_upgrade', kwargs={'external_id': self.reg.external_id}), {})
        self.assertEqual(response.status_code, 403)


class UpgradeViewTest(TestCase):
    def setUp(self):
        self.convention = create_test_convention()
        self.levels = create_test_registrationlevels(self.convention)
        self.upgrades = create_test_registrationupgrades([
            (self.levels['basic'], self.levels['sponsor']),
            (self.levels['basic'], self.levels['supersponsor']),
            (self.levels['sponsor'], self.levels['supersponsor']),
        ])
        self.payment_method = create_test_paymentmethod('Credit', True)
        self.reg = create_test_registration(self.levels['sponsor'])
        self.reg.status = 1
        self.reg.save()

        self.coupon = models.CouponCode.objects.create(
            convention=self.convention,
            code='one-dollar',
            discount=1,
            percent=False,
        )
        self.full_coupon = models.CouponCode.objects.create(
            convention=self.convention,
            code='full-discount',
            discount=100,
            percent=True,
        )

    def test_unauthenticated_noid(self):
        response = self.client.get(reverse('convention_upgrade'))
        self.assertEqual(response.status_code, 302)

    def test_unauthenticated_id(self):
        response = self.client.get(reverse('convention_upgrade', kwargs={'external_id': self.reg.external_id}))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Select your new registration level' in response.content)

    def test_authenticated_single(self):
        user = create_test_user(username='drykath', password='drykath')
        self.reg.user = user
        self.reg.save()
        response = self.client.login(username='drykath', password='drykath')
        self.assertEqual(response, True)

        response = self.client.get(reverse('convention_upgrade'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Select your new registration level' in response.content)

    def test_authenticated_multiple(self):
        user = create_test_user(username='drykath', password='drykath')
        self.reg.user = user
        self.reg.save()
        second_reg = create_test_registration(self.levels['sponsor'])
        second_reg.status = 1
        second_reg.user = user
        second_reg.save()
        response = self.client.login(username='drykath', password='drykath')
        self.assertEqual(response, True)

        response = self.client.get(reverse('convention_upgrade'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Select a registration to upgrade' in response.content)

        response = self.client.post(reverse('convention_upgrade'), {'registration': self.reg.id})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Select your new registration level' in response.content)

    def test_authenticated_ineligible(self):
        user = create_test_user(username='drykath', password='drykath')
        self.reg.registration_level = self.levels['supersponsor']
        self.reg.user = user
        self.reg.save()
        response = self.client.login(username='drykath', password='drykath')
        self.assertEqual(response, True)

        response = self.client.get(reverse('convention_upgrade'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'No upgrades available' in response.content)

    def test_authenticated_none(self):
        user = create_test_user(username='drykath', password='drykath')
        self.reg.status = 0
        self.reg.user = user
        self.reg.save()
        response = self.client.login(username='drykath', password='drykath')
        self.assertEqual(response, True)

        response = self.client.get(reverse('convention_upgrade'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'You have no registrations for the current year' in response.content)

    def test_post_step1_limited(self):
        self.levels['supersponsor'].limit = 1
        self.levels['supersponsor'].save()
        create_test_registration(self.levels['supersponsor'], status=1)

        data = {
            'registration': self.reg.id,
            'upgrade': self.upgrades[2].id,
            'payment_method': self.payment_method.id,
            'tos': 'on',
        }
        response = self.client.post(reverse('convention_upgrade', kwargs={'external_id': self.reg.external_id}), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(b'Please verify the following information is correct' in response.content)
        self.assertTrue(b'That registration level is no longer available' in response.content)

    def test_post_step1_invalid(self):
        # Need to re-accept TOS
        data = {
            'registration': self.reg.id,
            'upgrade': self.upgrades[2].id,
            'payment_method': self.payment_method.id,
        }
        response = self.client.post(reverse('convention_upgrade', kwargs={'external_id': self.reg.external_id}), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'This field is required' in response.content)

    def test_step1_valid(self):
        data = {
            'registration': self.reg.id,
            'upgrade': self.upgrades[2].id,
            'payment_method': self.payment_method.id,
            'tos': 'on',
        }
        response = self.client.post(reverse('convention_upgrade', kwargs={'external_id': self.reg.external_id}), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Please verify the following information is correct' in response.content)

        # one-dollar coupon discount
        data['coupon_code'] = self.coupon.code
        response = self.client.post(reverse('convention_upgrade', kwargs={'external_id': self.reg.external_id}), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['amount'], self.upgrades[2].price - self.coupon.discount)

        # Full coupon discount
        data['coupon_code'] = self.full_coupon.code
        response = self.client.post(reverse('convention_upgrade', kwargs={'external_id': self.reg.external_id}), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['amount'], 0)

    def test_step2_coupon(self):
        # Zero value registration due to coupon code
        data = {
            'registration': self.reg.id,
            'upgrade': self.upgrades[2].id,
            'payment_method': self.payment_method.id,
            'coupon_code': self.full_coupon.code,
            'tos': 'on',
        }
        # Hit step 1
        response = self.client.post(reverse('convention_upgrade', kwargs={'external_id': self.reg.external_id}), data)
        # Confirm in step 2
        data['confirm'] = 1
        response = self.client.post(reverse('convention_upgrade', kwargs={'external_id': self.reg.external_id}), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b'Successfully upgraded!' in response.content)
        self.assertEqual(len(mail.outbox), 1)

        # Should upgrade the registration
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.registration_level, self.levels['supersponsor'])

#    @mock.patch('stripe.Charge.create')
#    def test_step2_credit(self, create_mock):
#        self.payment_method.is_credit = True
#        self.payment_method.save()
#
#        class charge(object):
#            id = 'ch_1234'
#
#        create_mock.return_value = charge()
#
#        data = {
#            'registration': self.reg.id,
#            'upgrade': self.upgrades[2].id,
#            'payment_method': self.payment_method.id,
#            'stripeToken': 'foo',
#            'tos': 'on',
#        }
#        # Hit step 1
#        response = self.client.post(reverse('convention_upgrade', kwargs={'external_id': self.reg.external_id}), data)
#        # Confirm in step 2
#        data['confirm'] = 1
#        response = self.client.post(reverse('convention_upgrade', kwargs={'external_id': self.reg.external_id}), data)
#        self.assertEqual(response.status_code, 200)
#        self.assertTrue(b'Successfully upgraded!' in response.content)
#        self.assertEqual(len(mail.outbox), 1)
#
#        # Should upgrade the registration
#        self.reg.refresh_from_db()
#        self.assertEqual(self.reg.registration_level, self.levels['supersponsor'])

    def test_step2_neither(self):
        # Bug: A "confirmed" upgrade with neither coupon code nor stripe token could trigger an upgrade
        data = {
            'registration': self.reg.id,
            'upgrade': self.upgrades[2].id,
            'payment_method': self.payment_method.id,
            'tos': 'on',
        }
        # Hit step 1
        response = self.client.post(reverse('convention_upgrade', kwargs={'external_id': self.reg.external_id}), data)
        # Confirm in step 2
        data['confirm'] = 1
        response = self.client.post(reverse('convention_upgrade', kwargs={'external_id': self.reg.external_id}), data)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(b'Successfully upgraded!' in response.content)
        self.assertEqual(len(mail.outbox), 0)

        # Registration should not have been upgraded
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.registration_level, self.levels['sponsor'])

    def test_convention_closed(self):
        self.convention.registrationsettings.registration_open = False
        self.convention.registrationsettings.save()
        response = self.client.get(reverse('convention_upgrade', kwargs={'external_id': self.reg.external_id}))
        self.assertEqual(response.status_code, 403)
        response = self.client.post(reverse('convention_upgrade', kwargs={'external_id': self.reg.external_id}), {})
        self.assertEqual(response.status_code, 403)


# Util Function Tests

class UtilSimpleFeistelTest(TestCase):
    def test_feistel_self_inversion(self):
        i = random.randint(1, 1000000)
        self.assertEqual(i, simple_feistel(simple_feistel(i)))
