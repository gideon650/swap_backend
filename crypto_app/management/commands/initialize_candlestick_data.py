from django.core.management.base import BaseCommand
from django.utils import timezone
from crypto_app.models import SyntheticAsset  # Replace 'myapp' with your app name
from crypto_app.candlestick_service import CandlestickService

class Command(BaseCommand):
    help = 'Initialize candlestick data for existing assets'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--symbol',
            type=str,
            help='Initialize data for specific symbol only'
        )
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='Number of days of historical data to generate (default: 7)'
        )
    
    def handle(self, *args, **options):
        symbol = options.get('symbol')
        days = options.get('days', 7)
        
        if symbol:
            try:
                asset = SyntheticAsset.objects.get(symbol=symbol)
                assets = [asset]
                self.stdout.write(f"Initializing data for {symbol}")
            except SyntheticAsset.DoesNotExist:
                self.stdout.write(
                    self.style.ERROR(f"Asset with symbol {symbol} not found")
                )
                return
        else:
            assets = SyntheticAsset.objects.all()
            self.stdout.write(f"Initializing data for {assets.count()} assets")
        
        for asset in assets:
            try:
                self.stdout.write(f"Processing {asset.symbol}...")
                
                # This will trigger data generation if it doesn't exist
                CandlestickService.get_chart_data(asset, '15min')
                
                self.stdout.write(
                    self.style.SUCCESS(f"✓ Completed {asset.symbol}")
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"✗ Failed {asset.symbol}: {e}")
                )
        
        self.stdout.write(
            self.style.SUCCESS("Initialization complete!")
        )