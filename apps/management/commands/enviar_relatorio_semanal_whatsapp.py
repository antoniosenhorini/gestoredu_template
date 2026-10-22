import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.models import Aluno, AtestadoAluno, RegistroFaltaAluno, RegistroOcorrenciaAluno
from apps.services.whatsapp_service import (
    enviar_template_relatorio_semanal_faltas,
    enviar_template_relatorio_semanal_ocorrencias,
)

logger = logging.getLogger(__name__)

TELEFONE_ESCOLA = "(44997516860)"
TIPOS_OCORRENCIA_LEGADOS_FALTA = ("falta", "Falta")
ARQUIVO_CONTROLE = Path(settings.MEDIA_ROOT) / "relatorios_semanais_whatsapp" / "enviados.json"
ARQUIVO_LOCK = Path(settings.MEDIA_ROOT) / "relatorios_semanais_whatsapp" / "enviando.lock"


class Command(BaseCommand):
    help = "Envia relatorios semanais de faltas e ocorrencias por WhatsApp."

    def add_arguments(self, parser):
        parser.add_argument(
            "--data",
            type=str,
            help="Data de referencia no formato YYYY-MM-DD. A semana dessa data sera processada.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Mostra os relatorios que seriam enviados, sem disparar WhatsApp nem gravar o JSON.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if dry_run:
            self._processar_relatorios(options, dry_run=True)
            return

        if not self._criar_lock():
            self.stdout.write(
                self.style.WARNING(
                    f"Ja existe uma execucao do relatorio semanal em andamento. Lock: {ARQUIVO_LOCK}"
                )
            )
            return

        try:
            self._processar_relatorios(options, dry_run=False)
        finally:
            self._remover_lock()

    def _processar_relatorios(self, options, dry_run=False):
        data_referencia = self._data_referencia(options.get("data"))
        semana_inicio, semana_fim = self._periodo_semana(data_referencia)
        semana_chave = semana_inicio.strftime("%G-W%V")
        enviados = {} if dry_run else self._carregar_controle()

        self.stdout.write(
            f"Processando relatorio semanal de {semana_inicio:%d/%m/%Y} a {semana_fim:%d/%m/%Y}."
        )

        if dry_run:
            self.stdout.write("Modo dry-run: ignorando enviados.json e lock; nenhum WhatsApp sera enviado.")

        faltas_por_aluno = self._faltas_por_aluno(semana_inicio, semana_fim)
        ocorrencias_por_aluno = self._ocorrencias_por_aluno(semana_inicio, semana_fim)
        alunos_ids = sorted(set(faltas_por_aluno) | set(ocorrencias_por_aluno))
        alunos = Aluno.objects.filter(id__in=alunos_ids).select_related("turma")
        alunos_por_id = {aluno.id: aluno for aluno in alunos}

        total_enviados = 0
        total_ignorados = 0
        for aluno_id in alunos_ids:
            aluno = alunos_por_id.get(aluno_id)
            if not aluno:
                continue

            falta_info = faltas_por_aluno.get(aluno_id)
            if falta_info and falta_info["dias"] >= 2:
                chave = self._chave_envio(semana_chave, aluno_id, "faltas")
                enviado = self._enviar_relatorio_faltas(
                    aluno,
                    chave,
                    falta_info["total"],
                    enviados,
                    dry_run,
                )
                total_enviados += int(enviado)
                total_ignorados += int(not enviado)

            ocorrencia_info = ocorrencias_por_aluno.get(aluno_id)
            if ocorrencia_info and ocorrencia_info["total"] >= 2:
                chave = self._chave_envio(semana_chave, aluno_id, "ocorrencias")
                enviado = self._enviar_relatorio_ocorrencias(
                    aluno,
                    chave,
                    ocorrencia_info["ocorrencias"],
                    enviados,
                    dry_run,
                )
                total_enviados += int(enviado)
                total_ignorados += int(not enviado)

        if not dry_run:
            self._salvar_controle(enviados)
            self.stdout.write(self.style.SUCCESS(f"Relatorios enviados: {total_enviados}."))
            self.stdout.write(f"Relatorios ignorados: {total_ignorados}.")
            return

        self.stdout.write(self.style.SUCCESS(f"Relatorios simulados: {total_enviados}."))
        self.stdout.write(f"Relatorios ignorados: {total_ignorados}.")

    def _data_referencia(self, data_opcao):
        if data_opcao:
            return datetime.strptime(data_opcao, "%Y-%m-%d").date()
        return timezone.localdate()

    def _criar_lock(self):
        ARQUIVO_LOCK.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(ARQUIVO_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False

        with os.fdopen(fd, "w", encoding="utf-8") as arquivo:
            arquivo.write(f"pid={os.getpid()}\n")
            arquivo.write(f"iniciado_em={timezone.localtime().isoformat()}\n")
        return True

    def _remover_lock(self):
        try:
            ARQUIVO_LOCK.unlink()
        except FileNotFoundError:
            pass

    def _periodo_semana(self, data_referencia):
        semana_inicio = data_referencia - timedelta(days=data_referencia.weekday())
        semana_fim = semana_inicio + timedelta(days=4)
        return semana_inicio, semana_fim

    def _carregar_controle(self):
        if not ARQUIVO_CONTROLE.exists():
            return {}
        try:
            with ARQUIVO_CONTROLE.open("r", encoding="utf-8") as arquivo:
                dados = json.load(arquivo)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Nao foi possivel ler controle de relatorios semanais: %s", exc)
            return {}
        return dados if isinstance(dados, dict) else {}

    def _salvar_controle(self, enviados):
        ARQUIVO_CONTROLE.parent.mkdir(parents=True, exist_ok=True)
        with ARQUIVO_CONTROLE.open("w", encoding="utf-8") as arquivo:
            json.dump(enviados, arquivo, ensure_ascii=False, indent=2, sort_keys=True)

    def _faltas_por_aluno(self, semana_inicio, semana_fim):
        faltas_por_aluno = defaultdict(lambda: {"total": 0, "datas": set()})
        faltas = RegistroFaltaAluno.objects.filter(
            data__gte=semana_inicio,
            data__lte=semana_fim,
        ).select_related("aluno")
        atestados_por_aluno = self._atestados_ativos_por_aluno(semana_inicio, semana_fim)

        for falta in faltas:
            if self._falta_coberta_por_atestado(falta, atestados_por_aluno):
                continue
            dados = faltas_por_aluno[falta.aluno_id]
            dados["total"] += falta.quantidade_faltas or 1
            dados["datas"].add(falta.data)

        for dados in faltas_por_aluno.values():
            dados["dias"] = len(dados["datas"])

        return faltas_por_aluno

    def _atestados_ativos_por_aluno(self, semana_inicio, semana_fim):
        atestados_por_aluno = defaultdict(list)
        atestados = AtestadoAluno.objects.filter(
            ativo=True,
            data_inicio__lte=semana_fim,
            data_fim__gte=semana_inicio,
        )
        for atestado in atestados:
            atestados_por_aluno[atestado.aluno_id].append(atestado)
        return atestados_por_aluno

    def _falta_coberta_por_atestado(self, falta, atestados_por_aluno):
        for atestado in atestados_por_aluno.get(falta.aluno_id, []):
            if atestado.data_inicio <= falta.data <= atestado.data_fim:
                return True
        return False

    def _ocorrencias_por_aluno(self, semana_inicio, semana_fim):
        ocorrencias_por_aluno = defaultdict(lambda: {"total": 0, "ocorrencias": []})
        ocorrencias = (
            RegistroOcorrenciaAluno.objects.filter(
                data__gte=semana_inicio,
                data__lte=semana_fim,
            )
            .exclude(tipo_ocorrencia__in=TIPOS_OCORRENCIA_LEGADOS_FALTA)
            .select_related("aluno")
            .order_by("aluno__nome", "data", "horario_chegada", "id")
        )

        for ocorrencia in ocorrencias:
            dados = ocorrencias_por_aluno[ocorrencia.aluno_id]
            dados["total"] += 1
            dados["ocorrencias"].append(ocorrencia)

        return ocorrencias_por_aluno

    def _chave_envio(self, semana_chave, aluno_id, tipo_relatorio):
        return f"{semana_chave}:aluno:{aluno_id}:{tipo_relatorio}"

    def _enviar_relatorio_faltas(self, aluno, chave, total_faltas, enviados, dry_run):
        telefone = self._telefone_para_envio(aluno, chave, enviados, dry_run)
        if not telefone:
            return False

        total_aulas = total_faltas * 6
        data_envio = self._data_envio()
        if dry_run:
            self.stdout.write(
                f"[dry-run] Receberia relatorio de faltas: {aluno.nome} ({telefone}) - {total_faltas} falta(s)."
            )
            return True

        resultado = enviar_template_relatorio_semanal_faltas(
            telefone,
            aluno.nome,
            total_faltas,
            total_aulas,
            TELEFONE_ESCOLA,
            data_envio,
        )
        return self._registrar_resultado_envio(aluno, chave, telefone, resultado, enviados)

    def _enviar_relatorio_ocorrencias(self, aluno, chave, ocorrencias, enviados, dry_run):
        telefone = self._telefone_para_envio(aluno, chave, enviados, dry_run)
        if not telefone:
            return False

        lista_ocorrencias = " • ".join(self._linha_ocorrencia(ocorrencia) for ocorrencia in ocorrencias)
        data_envio = self._data_envio()
        if dry_run:
            self.stdout.write(
                f"[dry-run] Receberia relatorio de ocorrencias: {aluno.nome} ({telefone}) - {len(ocorrencias)} ocorrencia(s)."
            )
            return True

        resultado = enviar_template_relatorio_semanal_ocorrencias(
            telefone,
            aluno.nome,
            len(ocorrencias),
            lista_ocorrencias,
            TELEFONE_ESCOLA,
            data_envio,
        )
        return self._registrar_resultado_envio(aluno, chave, telefone, resultado, enviados)

    def _telefone_para_envio(self, aluno, chave, enviados, dry_run=False):
        if not dry_run and chave in enviados:
            self.stdout.write(f"Ignorado, ja enviado nesta semana: {chave}")
            return ""

        telefone = (aluno.telefone or "").strip()
        if not telefone:
            self.stdout.write(self.style.WARNING(f"Aluno sem telefone cadastrado: {aluno.nome}"))
            return ""

        return telefone

    def _registrar_resultado_envio(self, aluno, chave, telefone, resultado, enviados):
        if resultado.get("status"):
            enviados[chave] = {
                "aluno_id": aluno.id,
                "aluno_nome": aluno.nome,
                "telefone": resultado.get("numero") or telefone,
                "enviado_em": timezone.localtime().isoformat(),
            }
            self._salvar_controle(enviados)
            self.stdout.write(self.style.SUCCESS(f"Relatorio enviado para {aluno.nome}."))
            return True

        logger.warning(
            "Falha ao enviar relatorio semanal por WhatsApp.",
            extra={
                "aluno": aluno.nome,
                "aluno_id": aluno.id,
                "numero_usado": resultado.get("numero") or telefone,
                "erro_whatsapp": resultado.get("erro"),
                "resposta_whatsapp": resultado.get("resposta"),
                "status_code_whatsapp": resultado.get("status_code"),
            },
        )
        self.stdout.write(self.style.WARNING(f"Falha ao enviar relatorio para {aluno.nome}."))
        return False

    def _linha_ocorrencia(self, ocorrencia):
        tipo = ocorrencia.get_tipo_ocorrencia_display()
        descricao = (ocorrencia.motivo_alegado or ocorrencia.observacoes_adicionais or "").strip()
        if descricao:
            return f"- {ocorrencia.data:%d/%m/%Y} - {tipo}: {descricao}"
        return f"- {ocorrencia.data:%d/%m/%Y} - {tipo}"

    def _data_envio(self):
        return timezone.localtime().strftime("%d/%m/%Y %H:%M")
