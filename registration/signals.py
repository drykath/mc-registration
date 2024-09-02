from django.core.mail import send_mail
from django.dispatch import receiver
from django.db.models import Q
from django.db.models.signals import post_save
from django.template import loader
from .models import Registration, RegistrationHold, RegistrationSettings
from .utils import simple_feistel, stringify_integer

from convention import get_convention_model

# When a Convention object is created, auto-attach RegistrationSettings
@receiver(post_save, sender=get_convention_model())
def attach_conventionsettings(sender, **kwargs):
    if kwargs.get('created', False):
        con = kwargs.get('instance')
        con.registrationsettings = RegistrationSettings.objects.create(convention=con)
        con.save()

@receiver(post_save, sender=Registration)
def compute_external_id_field(sender, **kwargs):
    # Compute external_id for newly created registrations
    if kwargs.get('created', False):
        registration = kwargs.get('instance')
        registration.external_id = stringify_integer(simple_feistel(registration.id))
        registration.save()

@receiver(post_save, sender=Registration)
def check_registration_holds(sender, **kwargs):
    # Check new registrations against the list of holds
    ci_fields = ['first_name', 'last_name', 'badge_name', 'email',
                 'address', 'city', 'state', 'postal_code']
    exact_fields = ['birthday', 'ip']

    if kwargs.get('created', False):
        registration = kwargs.get('instance')
        holds = RegistrationHold.objects.all()
        matched = []
        notes_addition = ''
        private_notes_addition = ''
        private_check_in = False
        notify_registration_group = False
        notify_board_group = False

        # XXX: Any more efficient way of doing this?
        for hold in holds:
            any_matched = False
            all_matched = True
            for field in ci_fields:
                if getattr(hold, field):
                    if getattr(registration, field).lower() == getattr(hold, field).lower():
                        any_matched = True
                    else:
                        all_matched = False
            for field in exact_fields:
                if getattr(hold, field):
                    if getattr(registration, field) == getattr(hold, field):
                        any_matched = True
                    else:
                        all_matched = False

            if any_matched and all_matched:
                if hold.notes_addition:
                    notes_addition += 'Registration notes:\n{}\n\n'.format(hold.notes_addition)
                if hold.private_notes_addition:
                    private_notes_addition += 'Registration flagged:\n{}\n\n'.format(hold.private_notes_addition)
                if hold.private_check_in:
                    private_check_in = True
                matched.append(hold)
                if hold.notify_registration_group:
                    notify_registration_group = True
                if hold.notify_board_group:
                    notify_board_group = True

        # Look for duplicates matching last name, and either first name or birthday
        for other_reg in Registration.objects.exclude(pk=registration.pk).filter(
            Q(last_name=registration.last_name),
            Q(first_name=registration.first_name) | Q(birthday=registration.birthday)
        ):
            duplicate_note = 'Possible duplicate registration received, matching:\n{id} {external_id}\n{last_name} and {first_name} or {birthday}\n\n'.format(
                id=other_reg.id,
                external_id=other_reg.external_id,
                badge_name=other_reg.badge_name,
                last_name=other_reg.last_name,
                first_name=other_reg.first_name,
                birthday=other_reg.birthday,
            )
            notes_addition += duplicate_note
            matched.append(FakeHold(duplicate_note))
            notify_registration_group = True

        if matched:
            notes = registration.notes or ''
            registration.notes = notes + notes_addition
            private_notes = registration.private_notes or ''
            registration.private_notes = private_notes + private_notes_addition
            if not registration.private_check_in and private_check_in:
                registration.private_check_in = True
            registration.save()

            notification_list = []
            if notify_registration_group:
                notification_list.append('registration@yourconvention.org')
            if notify_board_group:
                notification_list.append('board@yourconvention.org')

            if notification_list:
                c = {
                    'registration': registration,
                    'holds': matched,
                }
                email_subject = loader.render_to_string(
                    'registration/held_registration_subject.txt', c
                )
                # Email subject *must not* contain newlines
                email_subject = ''.join(email_subject.splitlines())
                email_body = loader.render_to_string(
                    'registration/held_registration_body.txt', c
                )
                send_mail(email_subject, email_body,
                          'registration@yourconvention.org',
                          notification_list, fail_silently=True)


class FakeHold(object):
    notes_addition = ''

    def __init__(self, notes_addition):
        self.notes_addition = notes_addition
