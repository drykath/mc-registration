from django.core.management.base import BaseCommand, CommandError

from datetime import timedelta
from django.core.mail import send_mail
from django.utils import timezone
import stripe

from .models import Payment

class Command(BaseCommand):
    help = 'Process payments that have had refunds requested'

    def handle(self, *args, **options):
        payment_refund_requested = 2
        payment_refunded = 3
        delay_days = 3
        warning_days = 1
        cutoff = timezone.now() - timedelta(days=delay_days)
        warning = timezone.now() - timedelta(days=delay_days-warning_days)

        refunds_processed = []
        refunds_errors = []

        # Want to confirm production output before giving this teeth
        REALLY_PROCESS = True

        refunds = Payment.objects.filter(payment_state=payment_refund_requested,
                                         refund_requested__lt=cutoff)
        for payment in refunds:
            if payment.payment_method.is_credit and payment.payment_extra:
                try:
                    stripe.api_key = payment.registration.registration_level.convention.stripe_secret_key
                    charge = stripe.Charge.retrieve(payment.payment_extra)
                    if REALLY_PROCESS:
                        refund = charge.refunds.create()
                        payment.refund_processed = timezone.now()
                        payment.payment_state = payment_refunded
                        payment.save()
                        refunds_processed.append(payment)
                except stripe.error.StripeError as e:
                    print('Failed to refund %.02f payment by Stripe to payment ID %s (%s)' % (payment.payment_amount, payment.id, e.json_body['error']['message']), messages.ERROR)
                    refunds_errors.append('Failed to refund %.02f payment by Stripe to payment ID %s (%s)' % (payment.payment_amount, payment.id, e.json_body['error']['message']), messages.ERROR)

        # Gather future refunds to report to teasurer
        refunds_soon = Payment.objects.filter(payment_state=payment_refund_requested,
                                              refund_requested__lt=warning)


        if refunds_processed or refunds_errors or refunds_soon:
            body = 'This is a report detailing the delayed refund process.\n\n'
            if refunds_processed:
                body += 'These refunds have been processed with the payment provider:\n'
                for payment in refunds_processed:
                    body += "{last}, {first} ({badge}) - {level}, reason: {reason}\n".format(
                        last = payment.registration.last_name,
                        first = payment.registration.first_name,
                        badge = payment.registration.badge_name,
                        level = payment.registration.registration_level.title,
                        reason = payment.refund_reason
                    )
                body += '\n'

            if refunds_errors:
                body += 'These refunds were attempted, but errors occurred with the payment provider:\n'
                for payment in refunds_errors:
                    body += payment + '\n'
                body += '\n'

            if refunds_soon:
                body += 'These refunds are scheduled to be processed in {warning_days} day(s):\n'.format(
                    warning_days=warning_days
                )
                for payment in refunds_soon:
                    body += "{last}, {first} ({badge}) - {level}, reason: {reason}\n".format(
                        last = payment.registration.last_name,
                        first = payment.registration.first_name,
                        badge = payment.registration.badge_name,
                        level = payment.registration.registration_level.title,
                        reason = payment.refund_reason
                    )
                body += '\n'

            send_mail('Refund report for {date}'.format(
                    date=timezone.now().strftime('%x')
                ),
                body,
                'registration@motorcityfurrycon.org',
                ['registration@motorcityfurrycon.org', 'treasurer@motorcityfurrycon.org']
            )
