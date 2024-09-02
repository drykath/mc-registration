from django.core.management.base import BaseCommand, CommandError

import csv
import re
from datetime import datetime

from convention.models import Convention
from .models import (
    Registration, Payment, CouponCode, CouponUse, PaymentMethod,
    RegistrationLevel, ShirtSize
)

class Command(BaseCommand):
    help = 'Import registrations from a CSV file'

    def handle(self, *args, **options):
        # XXX: The CSV parser may need to be edited depending on the source
        if len(args) != 1:
            raise CommandError('Need CSV file as single argument')

        convention = Convention.objects.current()
        level = RegistrationLevel.objects.get(convention=convention, title='Sponsor')
        shirt_size = ShirtSize.objects.first()

        payment_method = PaymentMethod.objects.get(name='Complimentary')
        coupon = CouponCode.objects.get(code='Staff2016')

        with open(args[0], 'rb') as staff:
            for row in csv.DictReader(staff):
                if row['Imported'] == 'Yes':
                    continue

                first_name = ' '.join(row['Name'].split(' ')[:-1])
                last_name = row['Name'].split(' ')[-1]
                birthday = datetime.strptime(row['Birthday'], '%m/%d/%Y')

                # Import main registration entry
                reg = Registration(
                    first_name=first_name,
                    last_name=last_name,
                    badge_name=row['Badge Name'],
                    email=row['Email Address'],
                    birthday=birthday,
                    address=row['Address'],
                    city='',
                    state='',
                    postal_code='',
                    country='United States',
                    registration_level=level,
                    shirt_size=shirt_size,
                    status=1,
                    ip='127.0.0.1',
                )
                reg.save()

                # Create coupon usage and 0-amount payment records
                payment = Payment(
                    registration=reg,
                    payment_method=payment_method,
                    payment_level_comment='Imported registration',
                    payment_amount=0,
                )
                payment.save()
                couponuse = CouponUse(
                    registration=reg,
                    coupon=coupon,
                )
                couponuse.save()
