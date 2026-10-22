import shutil

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ._media_utils import (
    collect_media_files,
    create_backup,
    find_best_candidate,
    iter_references,
    media_root,
    relative_media_path,
    target_relative_path,
    unique_target_path,
    write_json,
)


class Command(BaseCommand):
    help = 'Corrige referencias quebradas de midia, com dry-run por padrao e backup antes do apply.'

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--dry-run', action='store_true', help='Mostra o que seria corrigido sem alterar nada.')
        mode.add_argument('--apply', action='store_true', help='Aplica correcoes seguras e cria backup antes.')
        parser.add_argument('--detalhado', action='store_true', help='Mostra tambem arquivos sem candidato encontrado.')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        detalhado = options['detalhado']
        root = media_root()
        self.stdout.write(f'MEDIA_ROOT: {root}')
        if not root.exists():
            self.stdout.write(self.style.WARNING('MEDIA_ROOT nao existe. Nenhuma correcao pode ser aplicada sem arquivos fisicos.'))

        files = collect_media_files()
        refs = list(iter_references())
        quebrados = [ref for ref in refs if not ref.absolute_path.exists()]
        planos = []
        ausentes = []

        for ref in quebrados:
            candidate, score = find_best_candidate(ref, files)
            if not candidate:
                ausentes.append({
                    'model': ref.model._meta.label,
                    'id': ref.obj.pk,
                    'field': ref.field.name,
                    'current': ref.name,
                    'score': score,
                })
                continue

            target_rel = target_relative_path(ref, candidate)
            target_abs = unique_target_path(target_rel)
            final_rel = relative_media_path(target_abs)
            needs_move = candidate.resolve() != target_abs.resolve()
            planos.append({
                'ref': ref,
                'candidate': candidate,
                'score': score,
                'target_abs': target_abs,
                'final_rel': final_rel,
                'needs_move': needs_move,
            })

        self.stdout.write(self.style.WARNING(f'Referencias quebradas encontradas: {len(quebrados)}'))
        self.stdout.write(self.style.SUCCESS(f'Correcoes possiveis: {len(planos)}'))
        self.stdout.write(self.style.WARNING(f'Arquivos ainda ausentes: {len(ausentes)}'))
        self.stdout.write('')

        if planos:
            self.stdout.write('Correcoes possiveis:')
            for plano in planos:
                ref = plano['ref']
                action = 'mover e atualizar' if plano['needs_move'] else 'atualizar caminho'
                self.stdout.write(
                    f"- {ref.model._meta.label} ID {ref.obj.pk} campo {ref.field.name}: "
                    f"{ref.name} -> {plano['final_rel']} "
                    f"(candidato={relative_media_path(plano['candidate'])}, score={plano['score']}, acao={action})"
                )
            self.stdout.write('')

        if detalhado or ausentes:
            self.stdout.write('Sem candidato seguro:')
            if not ausentes:
                self.stdout.write('- Nenhum')
            for item in ausentes:
                self.stdout.write(
                    f"- {item['model']} ID {item['id']} campo {item['field']}: {item['current']} "
                    f"(melhor score={item['score']})"
                )
            self.stdout.write('')

        if not apply_changes:
            self.stdout.write(self.style.NOTICE('Dry-run concluido. Nenhuma alteracao foi feita. Use --apply para corrigir.'))
            return

        if not planos:
            self.stdout.write(self.style.WARNING('Nenhuma correcao segura encontrada para aplicar.'))
            return

        backup_dir, db_backup, report_path = create_backup()
        self.stdout.write(f'Backup criado em: {backup_dir}')
        if db_backup:
            self.stdout.write(f'- Banco: {db_backup}')

        report = {
            'db_backup': str(db_backup) if db_backup else None,
            'correcoes': [],
            'ausentes': ausentes,
        }

        try:
            with transaction.atomic():
                for plano in planos:
                    ref = plano['ref']
                    candidate = plano['candidate']
                    target_abs = plano['target_abs']
                    final_rel = plano['final_rel']

                    file_backup = None
                    if plano['needs_move']:
                        file_backup = backup_dir / 'arquivos' / relative_media_path(candidate)
                        file_backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(candidate, file_backup)
                        target_abs.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(candidate), str(target_abs))

                    setattr(ref.obj, ref.field.name, final_rel)
                    ref.obj.save(update_fields=[ref.field.name])
                    report['correcoes'].append({
                        'model': ref.model._meta.label,
                        'id': ref.obj.pk,
                        'field': ref.field.name,
                        'old': ref.name,
                        'new': final_rel,
                        'candidate': relative_media_path(candidate),
                        'moved': plano['needs_move'],
                        'file_backup': str(file_backup) if file_backup else None,
                    })
        except Exception as exc:
            raise CommandError(f'Erro ao aplicar correcoes. Backup preservado em {backup_dir}. Detalhe: {exc}') from exc
        finally:
            write_json(report_path, report)

        self.stdout.write(self.style.SUCCESS(f'Correcoes aplicadas: {len(report["correcoes"])}'))
        self.stdout.write(f'Relatorio da aplicacao: {report_path}')
        if ausentes:
            self.stdout.write(self.style.WARNING('Alguns arquivos continuam ausentes e precisam ser reenviados manualmente.'))
