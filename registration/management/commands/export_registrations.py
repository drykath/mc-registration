from django.core.management.base import BaseCommand, CommandError

import csv
from datetime import datetime

from convention.models import Convention

from .models import Registration

class Command(BaseCommand):
    help = 'Export basic registration info to a CSV file'

    def handle(self, *args, **options):
        convention = Convention.objects.current()

        with open('registrations.csv', 'wb') as csvfile:
            fieldnames = ['first_name', 'last_name', 'badge_name', 'badge_id', 'paid', 'checked_in']
            regwriter = csv.DictWriter(csvfile, fieldnames=fieldnames)
            regwriter.writeheader()
            for row in Registration.objects.filter(registration_level__convention=convention):

                first_name = row.first_name.encode('utf-8')
                last_name = row.last_name.encode('utf-8')
                badge_name = row.badge_name.encode('utf-8')
                paid = row.status == 1
                checked_in = row.checked_in
                badges = row.badgeassignment_set.all().order_by('-id')
                if len(badges) == 0:
                    regwriter.writerow({'first_name': first_name, 'last_name': last_name, 'badge_name': badge_name, 'badge_id': 'None', 'paid': paid, 'checked_in': checked_in})
                else:
                    regwriter.writerow({'first_name': first_name, 'last_name': last_name, 'badge_name': badge_name, 'badge_id': badges[0].id, 'paid': paid, 'checked_in': checked_in})
                    for addl_badge in badges[1:]:
                        regwriter.writerow({'first_name': 'Registration was previously assigned ID', 'last_name': '', 'badge_name': '', 'badge_id': addl_badge.id, 'paid': '', 'checked_in': ''})
