from django.apps import AppConfig

class RegisterConfig(AppConfig):
    name = 'registration'
    verbose_name = 'Registration'

    def ready(self):
        import registration.signals
