from django.core.management.base import BaseCommand, CommandError

from datetime import timedelta
from django.utils import timezone

from .models import RegistrationTempAvatar

class Command(BaseCommand):
    help = 'Remove uploaded avatars that never made it into a registration'

    def handle(self, *args, **options):
        grace_period_days = 7
        cutoff = timezone.now() - timedelta(days=grace_period_days)

        images = RegistrationTempAvatar.objects.filter(uploaded__lt=cutoff)
        for image in images:
            image.delete()
