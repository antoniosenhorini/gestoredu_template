from django.core.management.base import BaseCommand

from apps.media_utils import garantir_pastas_media


class Command(BaseCommand):
    help = 'Cria MEDIA_ROOT e as subpastas usadas pelos uploads do sistema.'

    def handle(self, *args, **options):
        caminhos = garantir_pastas_media()
        self.stdout.write(self.style.SUCCESS(f'Pastas de media verificadas/criadas: {len(caminhos)}'))
        for caminho in caminhos:
            self.stdout.write(f'- {caminho}')
