from django.apps import AppConfig


class CryptoAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'crypto_app'


    def ready(self):
        import crypto_app.signals  # Replace 'your_app_name' with your app's name
