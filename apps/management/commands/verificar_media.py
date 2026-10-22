from django.core.management.base import BaseCommand

from ._media_utils import collect_media_files, iter_media_fields, iter_references, media_root, relative_media_path


class Command(BaseCommand):
    help = 'Verifica arquivos referenciados no banco, arquivos ausentes e arquivos orfaos em MEDIA_ROOT.'

    def add_arguments(self, parser):
        parser.add_argument('--detalhado', action='store_true', help='Mostra todos os arquivos OK, quebrados e orfaos.')

    def handle(self, *args, **options):
        detalhado = options['detalhado']
        root = media_root()
        self.stdout.write(f'MEDIA_ROOT: {root}')
        self.stdout.write(f'MEDIA_URL: /media/')
        self.stdout.write('')

        self.stdout.write('Campos de midia encontrados:')
        for model, field in iter_media_fields():
            upload_to = field.upload_to if not callable(field.upload_to) else '<callable>'
            self.stdout.write(f'- {model._meta.label}.{field.name} -> upload_to={upload_to}')
        self.stdout.write('')

        refs = list(iter_references())
        ok = []
        quebrados = []
        referenced = set()

        for ref in refs:
            referenced.add(ref.relative_path.as_posix())
            item = {
                'model': ref.model._meta.label,
                'id': ref.obj.pk,
                'field': ref.field.name,
                'name': ref.name,
                'path': str(ref.absolute_path),
            }
            if ref.absolute_path.exists():
                ok.append(item)
            else:
                quebrados.append(item)

        files = collect_media_files()
        orfaos = []
        for path in files:
            rel = relative_media_path(path)
            if rel not in referenced:
                orfaos.append({'name': rel, 'path': str(path), 'size': path.stat().st_size})

        self.stdout.write(self.style.SUCCESS(f'Arquivos OK: {len(ok)}'))
        self.stdout.write(self.style.WARNING(f'Referencias quebradas: {len(quebrados)}'))
        self.stdout.write(self.style.NOTICE(f'Arquivos orfaos: {len(orfaos)}'))
        self.stdout.write('')

        if detalhado or quebrados:
            self.stdout.write('Referencias quebradas:')
            if not quebrados:
                self.stdout.write('- Nenhuma')
            for item in quebrados:
                self.stdout.write(f"- {item['model']} ID {item['id']} campo {item['field']}: {item['name']} -> {item['path']}")
            self.stdout.write('')

        if detalhado:
            self.stdout.write('Arquivos OK:')
            if not ok:
                self.stdout.write('- Nenhum')
            for item in ok:
                self.stdout.write(f"- {item['model']} ID {item['id']} campo {item['field']}: {item['name']}")
            self.stdout.write('')

            self.stdout.write('Arquivos orfaos:')
            if not orfaos:
                self.stdout.write('- Nenhum')
            for item in orfaos:
                self.stdout.write(f"- {item['name']} ({item['size']} bytes)")
