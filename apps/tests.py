import importlib
import re
import tempfile
from datetime import date, time
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import django.apps
from django.contrib.auth.models import Group, User
from django.core import mail
from django.core.management import call_command
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook
import pandas as pd

from .management.commands.enviar_relatorio_semanal_whatsapp import Command as RelatorioSemanalWhatsappCommand
from .models import (
    AgendamentoLab, Aluno, AtestadoAluno, AvisoProfessor, ConfiguracaoCronogramaLaboratorio,
    DivergenciaCronogramaPdf, HistoricoAlteracaoCronogramaLab, HorarioProfessorPDF, Laboratorio,
    Professor, ProfessorPdfAlias, RegistroFaltaAluno, RegistroOcorrenciaAluno,
    SemanaCronogramaLaboratorio, Turma, TurmaPdfAlias,
)
from .services.horario_pdf_comparacao import gerar_divergencias
from .services.horario_pdf_matching import extrair_chave_turma, normalizar
from .services.horario_pdf_parser import ler_pdf_horario_professores
from .services.whatsapp_service import (
    enviar_mensagem_whatsapp,
    enviar_template_aviso_falta_aluno,
    enviar_template_aviso_ocorrencia_aluno,
    formatar_numero_whatsapp,
)
from .views.faltas import _telefone_whatsapp_aluno
from .views.laboratorios import obter_cronograma_ativo
from .forms import PEDAGOGA_NAO_DEFINIDA, normalizar_turma_mapeamento, pedagoga_por_turma


class SegurancaSenhaTests(TestCase):
    def setUp(self):
        self.grupo_equipe = Group.objects.create(name='Equipe Diretiva')
        self.grupo_professores = Group.objects.create(name='Professores')

    def test_home_mostra_aviso_para_professor_com_senha_padrao(self):
        user = User.objects.create_user(username='professor-seguranca', password='zanchim2026')
        user.groups.add(self.grupo_professores)
        Professor.objects.create(usuario=user, nome_completo='Professor Seguranca', ativo=True)
        self.client.force_login(user)

        response = self.client.get(reverse('home'))

        self.assertContains(response, 'Por seguranca, altere sua senha pessoal.')
        self.assertContains(response, reverse('alterar_senha'))
        self.assertContains(response, reverse('recuperar_senha'))

    def test_painel_mostra_aviso_para_equipe_com_senha_padrao(self):
        user = User.objects.create_user(username='equipe-seguranca', password='zanchim2026')
        user.groups.add(self.grupo_equipe)
        self.client.force_login(user)

        response = self.client.get(reverse('painel_equipe'))

        self.assertContains(response, 'Por seguranca, altere sua senha pessoal.')
        self.assertContains(response, reverse('alterar_senha'))
        self.assertContains(response, reverse('recuperar_senha'))

    def test_aviso_nao_aparece_para_superuser(self):
        user = User.objects.create_superuser(username='admin-seguranca', password='zanchim2026')
        self.client.force_login(user)

        response = self.client.get(reverse('home'))

        self.assertNotContains(response, 'Por seguranca, altere sua senha pessoal.')

    def test_alterar_senha_funciona_para_usuario_logado(self):
        user = User.objects.create_user(username='troca-senha', password='zanchim2026')
        self.client.force_login(user)

        response = self.client.post(
            reverse('alterar_senha'),
            {
                'old_password': 'zanchim2026',
                'new_password1': 'SenhaForte2026!',
                'new_password2': 'SenhaForte2026!',
            },
        )

        self.assertRedirects(response, reverse('senha_alterada'))
        user.refresh_from_db()
        self.assertTrue(user.check_password('SenhaForte2026!'))

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_recuperacao_de_senha_envia_email_com_link(self):
        User.objects.create_user(
            username='recuperar-senha',
            email='professor@example.com',
            password='zanchim2026',
        )

        response = self.client.post(reverse('recuperar_senha'), {'email': 'professor@example.com'})

        self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('/conta/redefinir/', mail.outbox[0].body)


class AvisoProfessorTests(TestCase):
    def test_post_aviso_professor_cria_registro(self):
        response = self.client.post(reverse('aviso_professor'), {
            'professor_nome': 'Maria Professora',
            'disciplina': 'Matematica',
            'data': '2026-06-05',
            'tipo': 'FALTA',
            'observacao': 'Aviso de falta',
        })

        self.assertRedirects(response, reverse('aviso_professor'))
        aviso = AvisoProfessor.objects.get()
        self.assertEqual(aviso.professor_nome, 'Maria Professora')
        self.assertEqual(aviso.disciplina, 'Matematica')
        self.assertEqual(aviso.data, date(2026, 6, 5))
        self.assertEqual(aviso.tipo, 'FALTA')
        self.assertEqual(aviso.observacao, 'Aviso de falta')


class FaltasOcorrenciasSeparacaoTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='digitador',
            password='senha-teste',
        )
        digitadores = Group.objects.create(name='Digitadores')
        self.user.groups.add(digitadores)
        equipe = Group.objects.create(name='Equipe Diretiva')
        self.equipe_user = User.objects.create_user(
            username='equipe',
            password='senha-teste',
        )
        self.equipe_user.groups.add(equipe)

        self.turma = Turma.objects.create(
            nome='1A',
            ano=2026,
            serie='1 Ano',
            ativa=True,
            turno='manha',
        )
        self.aluno = Aluno.objects.create(
            nome='Aluno Teste',
            numero=7,
            turma=self.turma,
        )

    def test_falta_aluno_fica_apenas_em_registro_falta_aluno(self):
        falta = RegistroFaltaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 15),
            quantidade_faltas=1,
            pedagoga='Sonia',
            registrado_por=self.user,
        )

        self.assertTrue(RegistroFaltaAluno.objects.filter(pk=falta.pk).exists())
        self.assertFalse(
            RegistroOcorrenciaAluno.objects.filter(
                aluno=self.aluno,
                data=falta.data,
            ).exists()
        )

    def test_ocorrencia_aluno_fica_apenas_em_registro_ocorrencia_aluno(self):
        ocorrencia = RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 15),
            tipo_ocorrencia='atraso',
            faltou=False,
            turno='manha',
            atendido_por='Sonia',
            registrado_por=self.user,
        )

        self.assertTrue(RegistroOcorrenciaAluno.objects.filter(pk=ocorrencia.pk).exists())
        self.assertFalse(
            RegistroFaltaAluno.objects.filter(
                aluno=self.aluno,
                data=ocorrencia.data,
            ).exists()
        )

    def test_fluxo_ocorrencia_digitador_rejeita_tipo_falta(self):
        self.client.force_login(self.user)

        for tipo_ocorrencia in ('falta', 'Falta'):
            with self.subTest(tipo_ocorrencia=tipo_ocorrencia):
                response = self.client.post(
                    reverse('formulario_digitador'),
                    {
                        'turma': self.turma.pk,
                        'numero_aluno': self.aluno.numero,
                        'nome_aluno': self.aluno.nome,
                        'data': '2026-05-15',
                        'turno': 'manha',
                        'faltou': '',
                        'tipo_ocorrencia': tipo_ocorrencia,
                        'motivo_alegado': 'Teste de tipo invalido',
                        'atendido_por': 'Sonia',
                        'responsavel_contatado': '',
                        'horario_chegada': '',
                        'horario_contato': '',
                        'alegado_responsavel': '',
                    },
                )

                self.assertRedirects(response, reverse('formulario_digitador'))
                self.assertFalse(RegistroOcorrenciaAluno.objects.filter(aluno=self.aluno).exists())
                self.assertFalse(
                    RegistroFaltaAluno.objects.filter(
                        aluno=self.aluno,
                        data=date(2026, 5, 15),
                    ).exists()
                )

    def test_fluxo_ocorrencia_real_rejeita_tipo_falta_maiusculo(self):
        response = self._post_ocorrencia_registrar('Falta')

        self.assertRedirects(response, reverse('registrar_ocorrencia_aluno'))
        self.assertFalse(RegistroOcorrenciaAluno.objects.filter(aluno=self.aluno).exists())
        self.assertFalse(RegistroFaltaAluno.objects.filter(aluno=self.aluno).exists())

    def test_mapeamento_pedagoga_por_turma(self):
        casos = [
            ('1A humanas', '', 'SONIA'),
            ('1º A Humanas', '', 'SONIA'),
            ('1 A HUM', '', 'SONIA'),
            ('1B exatas', '', 'SONIA'),
            ('1A TEC', '', 'SONIA'),
            ('1C agro', '', 'WANDA'),
            ('1 C AGRO', '', 'WANDA'),
            ('', '1º A Humanas', 'SONIA'),
            ('', '1 C AGRO', 'WANDA'),
            ('2A', 'Tecnico', 'VERONICA'),
            ('2B qualquer', '', 'VERONICA'),
            ('3A', '3 Ano', 'ELAINE'),
            ('3C qualquer', '', 'ELAINE'),
            ('9B', '9 Ano', 'WANDA'),
            ('7F', '7 Ano', 'ZINGARA'),
            ('5A', '5 Ano', PEDAGOGA_NAO_DEFINIDA),
        ]

        for nome, serie, esperado in casos:
            with self.subTest(nome=nome, serie=serie):
                turma = Turma(nome=nome, serie=serie)
                self.assertEqual(pedagoga_por_turma(turma), esperado)

    def test_normalizacao_turma_para_mapeamento(self):
        turma = Turma(nome='1º  A   Humanas', serie='Ensino Médio')

        self.assertEqual(normalizar_turma_mapeamento(turma), '1 a humanas ensino medio')

    def test_registrar_falta_define_pedagoga_pela_turma(self):
        self.turma.nome = '7F'
        self.turma.serie = '7 Ano'
        self.turma.save(update_fields=['nome', 'serie'])

        response = self._post_registrar_falta(possui_atestado='sim')

        falta = RegistroFaltaAluno.objects.get(aluno=self.aluno)
        self.assertEqual(falta.pedagoga, 'ZINGARA')
        self.assertContains(response, 'Falta registrada com sucesso.')

    def test_registrar_ocorrencia_define_pedagoga_pela_turma(self):
        self.turma.nome = '9B'
        self.turma.serie = '9 Ano'
        self.turma.save(update_fields=['nome', 'serie'])
        self.aluno.telefone = ''
        self.aluno.save(update_fields=['telefone'])

        self._post_ocorrencia_registrar('atraso')

        ocorrencia = RegistroOcorrenciaAluno.objects.get(aluno=self.aluno)
        self.assertEqual(ocorrencia.atendido_por, 'WANDA')

    def test_digitador_define_pedagoga_pela_turma(self):
        self.turma.nome = '8A'
        self.turma.serie = '8 Ano'
        self.turma.save(update_fields=['nome', 'serie'])

        self._post_ocorrencia_digitador('uniforme')

        ocorrencia = RegistroOcorrenciaAluno.objects.get(aluno=self.aluno)
        self.assertEqual(ocorrencia.atendido_por, 'ELAINE')

    def test_turma_sem_mapeamento_permite_pedagoga_manual(self):
        self.turma.nome = '5A'
        self.turma.serie = '5 Ano'
        self.turma.save(update_fields=['nome', 'serie'])

        response = self._post_registrar_falta(possui_atestado='sim')

        falta = RegistroFaltaAluno.objects.get(aluno=self.aluno)
        self.assertEqual(falta.pedagoga, 'Sonia')
        self.assertContains(response, 'Falta registrada com sucesso.')

    def test_painel_nao_conta_faltas_historicas_como_ocorrencias(self):
        self.client.force_login(self.equipe_user)
        RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 15),
            tipo_ocorrencia='falta',
            faltou=True,
            turno='manha',
            atendido_por='Sonia',
            registrado_por=self.user,
        )
        RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 15),
            tipo_ocorrencia='Falta',
            faltou=True,
            turno='manha',
            atendido_por='Sonia',
            registrado_por=self.user,
        )
        RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 15),
            tipo_ocorrencia='atraso',
            faltou=False,
            turno='manha',
            atendido_por='Sonia',
            registrado_por=self.user,
        )

        response = self.client.get(reverse('painel_equipe'), {
            'dia': '15',
            'mes': '5',
            'ano': '2026',
            'dia_ocorrencias_ranking': '15',
            'mes_ocorrencias_ranking': '5',
            'ano_ocorrencias_ranking': '2026',
        })

        self.assertEqual(response.context['total_ocorrencias_periodo'], 1)
        self.assertEqual(response.context['ocorrencias_por_dia'], [1])
        self.assertEqual(response.context['ranking_tipos_ocorrencia'], [{'tipo': 'Atraso', 'total': 1}])
        self.assertEqual(sum(response.context['ocorrencias_totais_manha']), 1)
        self.assertNotIn('Falta', response.context['ranking_tipos_ocorrencia_labels_json'])
        self.assertNotIn('falta', response.context['ranking_tipos_ocorrencia_labels_json'])

    def test_conferencia_ocorrencias_nao_lista_falta_no_filtro_nem_na_tabela(self):
        self.client.force_login(self.equipe_user)
        RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 15),
            tipo_ocorrencia='falta',
            faltou=True,
            turno='manha',
            atendido_por='Sonia',
            registrado_por=self.user,
        )
        RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 15),
            tipo_ocorrencia='atraso',
            faltou=False,
            turno='manha',
            atendido_por='Sonia',
            registrado_por=self.user,
        )

        response = self.client.get(reverse('conferencia_ocorrencias'), {'data': '2026-05-15'})

        self.assertEqual(len(response.context['ocorrencias']), 1)
        self.assertEqual(response.context['ocorrencias'][0].tipo_ocorrencia, 'atraso')
        self.assertNotIn(('falta', 'Falta'), response.context['tipos_ocorrencia'])
        self.assertNotContains(response, 'value="falta"')

    def test_filtro_relatorio_ocorrencias_nao_oferece_falta(self):
        self.client.force_login(self.equipe_user)

        response = self.client.get(reverse('relatorio_filtro'))

        self.assertNotContains(response, 'value="falta"')

    def test_relatorio_ocorrencias_nao_exporta_faltas_historicas(self):
        self.client.force_login(self.equipe_user)
        RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 15),
            tipo_ocorrencia='Falta',
            faltou=True,
            turno='manha',
            atendido_por='Sonia',
            registrado_por=self.user,
        )
        RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 15),
            tipo_ocorrencia='atraso',
            faltou=False,
            turno='manha',
            atendido_por='Sonia',
            registrado_por=self.user,
        )

        response = self.client.get(reverse('relatorio_ocorrencias_alunos'), {'mes': '5', 'ano': '2026'})
        workbook = load_workbook(BytesIO(response.content))
        valores = [
            cell
            for row in workbook.active.iter_rows(values_only=True)
            for cell in row
            if cell is not None
        ]

        self.assertIn('Atraso', valores)
        self.assertNotIn('Falta', valores)

    def _post_registrar_falta(self, possui_atestado='nao', follow=True):
        self.client.force_login(self.user)
        return self.client.post(
            reverse('registrar_falta_aluno'),
            {
                'aluno': self.aluno.pk,
                'turma': self.turma.pk,
                'turno_opcao': self.turma.turno,
                'data': '2026-05-15',
                'quantidade': '1',
                'pedagoga': 'Sonia',
                'possui_atestado': possui_atestado,
                'observacoes': 'Teste de aviso',
            },
            follow=follow,
        )

    def _mensagem_aluno_turma(self, aluno=None):
        aluno = aluno or self.aluno
        return f'Aluno: {aluno.nome} - Turma: {aluno.turma}.'

    def test_falta_com_atestado_marca_justificada_e_confirma_registro(self):
        response = self._post_registrar_falta(possui_atestado='sim')

        falta = RegistroFaltaAluno.objects.get(aluno=self.aluno)
        self.assertTrue(falta.justificada)
        self.assertContains(response, 'Falta registrada com sucesso.')
        self.assertContains(response, self._mensagem_aluno_turma())

    def test_falta_sem_telefone_salva_normalmente_com_mensagem_padrao(self):
        response = self._post_registrar_falta(possui_atestado='nao')

        self.assertTrue(RegistroFaltaAluno.objects.filter(aluno=self.aluno).exists())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Falta registrada com sucesso.')
        self.assertContains(response, self._mensagem_aluno_turma())
        self.assertEqual(self.client.session['ultimo_turno_falta_aluno'], self.turma.turno)
        self.assertEqual(self.client.session['ultima_turma_falta_aluno'], str(self.turma.pk))

    def test_falta_sem_telefone_mantem_exatamente_turma_por_id_com_nomes_parecidos(self):
        turma_exatas = Turma.objects.create(
            nome='3C',
            ano=2026,
            serie='Exatas',
            ativa=True,
            turno='manha',
        )
        turma_sistemas = Turma.objects.create(
            nome='3C',
            ano=2026,
            serie='Sistemas',
            ativa=True,
            turno='manha',
        )
        aluno_sistemas = Aluno.objects.create(
            nome='Aluno Sistemas',
            numero=12,
            turma=turma_sistemas,
            telefone='',
        )
        self.client.force_login(self.user)

        response_post = self.client.post(
            reverse('registrar_falta_aluno'),
            {
                'aluno': aluno_sistemas.pk,
                'turma': turma_sistemas.pk,
                'turno_opcao': turma_sistemas.turno,
                'data': '2026-05-15',
                'quantidade': '1',
                'pedagoga': 'Sonia',
                'possui_atestado': 'nao',
                'observacoes': 'Teste com turmas parecidas',
            },
            follow=False,
        )
        response = self.client.get(reverse('registrar_falta_aluno'))

        self.assertRedirects(response_post, reverse('registrar_falta_aluno'), fetch_redirect_response=False)
        self.assertTrue(RegistroFaltaAluno.objects.filter(aluno=aluno_sistemas).exists())
        self.assertContains(response, f'Aluno: {aluno_sistemas.nome} - Turma: {turma_sistemas}.')
        self.assertNotEqual(turma_exatas.pk, turma_sistemas.pk)
        self.assertEqual(self.client.session['ultimo_turno_falta_aluno'], turma_sistemas.turno)
        self.assertEqual(self.client.session['ultima_turma_falta_aluno'], str(turma_sistemas.pk))
        self.assertEqual(response.context['ultima_turma_falta_aluno'], str(turma_sistemas.pk))
        self.assertNotEqual(response.context['ultima_turma_falta_aluno'], str(turma_exatas.pk))

    def test_telefone_whatsapp_usa_campo_telefone_do_aluno(self):
        aluno = SimpleNamespace(telefone='5544999501967')

        self.assertEqual(_telefone_whatsapp_aluno(aluno), '5544999501967')

    def test_telefone_whatsapp_ignora_telefone_responsavel_no_fluxo_de_falta(self):
        aluno = SimpleNamespace(telefone_responsavel='5544999501967', telefone='')

        self.assertEqual(_telefone_whatsapp_aluno(aluno), '')

    def test_falta_com_telefone_cadastrado_exibe_mensagem_de_relatorio_semanal(self):
        self.aluno.telefone = '(44) 99999-0000'
        self.aluno.save(update_fields=['telefone'])

        response = self._post_registrar_falta(possui_atestado='nao')

        self.assertTrue(RegistroFaltaAluno.objects.filter(aluno=self.aluno).exists())
        self.assertContains(response, 'Falta registrada com sucesso.')
        self.assertContains(response, 'incluído no relatório semanal')
        self.assertContains(response, self._mensagem_aluno_turma())
        self.assertEqual(self.client.session['ultimo_turno_falta_aluno'], self.turma.turno)
        self.assertEqual(self.client.session['ultima_turma_falta_aluno'], str(self.turma.pk))

    def test_falta_duplicada_mantem_turma_turno_na_sessao_e_mostra_aluno_turma(self):
        RegistroFaltaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 15),
            quantidade_faltas=1,
            pedagoga='Sonia',
            registrado_por=self.user,
        )

        response = self._post_registrar_falta(possui_atestado='nao')

        self.assertEqual(RegistroFaltaAluno.objects.filter(aluno=self.aluno, data=date(2026, 5, 15)).count(), 1)
        self.assertContains(response, 'Este aluno ja possui falta registrada nesta data.')
        self.assertContains(response, self._mensagem_aluno_turma())
        self.assertEqual(self.client.session['ultimo_turno_falta_aluno'], self.turma.turno)
        self.assertEqual(self.client.session['ultima_turma_falta_aluno'], str(self.turma.pk))

    def test_registrar_falta_aluno_numero_chamada_nao_usa_type_number(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('registrar_falta_aluno'))

        self.assertContains(response, 'id="numero_aluno"')
        self.assertContains(response, 'type="text"')
        self.assertContains(response, 'inputmode="numeric"')
        self.assertContains(response, 'pattern="[0-9]*"')
        self.assertNotContains(response, 'id="numero_aluno" class="form-control" min="1"')

    def test_registrar_falta_aluno_template_tem_confirmacao_no_submit(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('registrar_falta_aluno'))

        self.assertContains(response, "form.addEventListener('submit'")
        self.assertContains(response, 'window.confirm(`Deseja realmente lançar falta para')
        self.assertContains(response, 'turmaHidden.value = alunoLocalizado.turma_id')
        self.assertContains(response, 'turnoOpcaoHidden.value = alunoLocalizado.turno')
        self.assertContains(response, 'name="turno_opcao_visual"')
        self.assertContains(response, "if (ultimaTurmaSalva)")
        self.assertContains(response, "console.log('Turma salva da sessão:', ultimaTurmaSalva)")

    def test_ocorrencia_digitador_piercing_exibe_mensagem_de_relatorio_semanal(self):
        self.client.force_login(self.user)
        self.aluno.telefone = '(44) 99999-0000'
        self.aluno.save(update_fields=['telefone'])

        response = self.client.post(
            reverse('formulario_digitador'),
            {
                'turma': self.turma.pk,
                'numero_aluno': self.aluno.numero,
                'nome_aluno': self.aluno.nome,
                'data': '2026-05-15',
                'turno': 'manha',
                'faltou': '',
                'tipo_ocorrencia': 'piercing',
                'motivo_alegado': 'Uso de piercing',
                'atendido_por': 'Sonia',
                'responsavel_contatado': '',
                'horario_chegada': '',
                'horario_contato': '',
                'alegado_responsavel': '',
            },
            follow=True,
        )

        self.assertTrue(RegistroOcorrenciaAluno.objects.filter(aluno=self.aluno, tipo_ocorrencia='piercing').exists())
        self.assertContains(response, 'incluído no relatório semanal')

    def test_ocorrencia_digitador_sem_telefone_salva_normalmente(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('formulario_digitador'),
            {
                'turma': self.turma.pk,
                'numero_aluno': self.aluno.numero,
                'nome_aluno': self.aluno.nome,
                'data': '2026-05-15',
                'turno': 'manha',
                'faltou': '',
                'tipo_ocorrencia': 'uniforme',
                'motivo_alegado': 'Uniforme',
                'atendido_por': 'Sonia',
                'responsavel_contatado': '',
                'horario_chegada': '',
                'horario_contato': '',
                'alegado_responsavel': '',
            },
            follow=True,
        )

        self.assertTrue(RegistroOcorrenciaAluno.objects.filter(aluno=self.aluno, tipo_ocorrencia='uniforme').exists())
        self.assertContains(response, 'incluído no relatório semanal')

    def _post_ocorrencia_digitador(self, tipo_ocorrencia, follow=True, enviar_reincidencia=''):
        self.client.force_login(self.user)
        return self.client.post(
            reverse('formulario_digitador'),
            {
                'turma': self.turma.pk,
                'numero_aluno': self.aluno.numero,
                'nome_aluno': self.aluno.nome,
                'data': '2026-05-15',
                'turno': 'manha',
                'faltou': '',
                'tipo_ocorrencia': tipo_ocorrencia,
                'motivo_alegado': 'Teste de ocorrencia',
                'atendido_por': 'Sonia',
                'responsavel_contatado': '',
                'horario_chegada': '',
                'horario_contato': '',
                'alegado_responsavel': '',
                'enviar_whatsapp_reincidencia': enviar_reincidencia,
            },
            follow=follow,
        )

    def _post_ocorrencia_registrar(self, tipo_ocorrencia, follow=True, enviar_reincidencia=''):
        self.client.force_login(self.user)
        return self.client.post(
            reverse('registrar_ocorrencia_aluno'),
            {
                'pedagoga': 'Sonia',
                'turma': self.turma.pk,
                'numero_aluno': self.aluno.numero,
                'nome_aluno': self.aluno.nome,
                'data': '2026-05-15',
                'turno': 'manha',
                'faltou': '',
                'tipo_ocorrencia': tipo_ocorrencia,
                'motivo_alegado': 'Teste de ocorrencia',
                'responsavel_contatado': '',
                'horario_chegada': '',
                'horario_contato': '',
                'alegado_responsavel': '',
                'observacoes_adicionais': '',
                'enviar_whatsapp_reincidencia': enviar_reincidencia,
            },
            follow=follow,
        )

    def test_ocorrencia_atraso_rota_real_registra_com_sucesso(self):
        self.aluno.telefone = '(44) 99999-0000'
        self.aluno.save(update_fields=['telefone'])

        response = self._post_ocorrencia_registrar(' atraso ')

        self.assertTrue(RegistroOcorrenciaAluno.objects.filter(aluno=self.aluno, tipo_ocorrencia='atraso').exists())
        self.assertContains(response, 'incluído no relatório semanal')

    def test_ocorrencia_atraso_rota_real_segunda_vez_nao_duplica(self):
        self.aluno.telefone = '(44) 99999-0000'
        self.aluno.save(update_fields=['telefone'])
        RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 15),
            tipo_ocorrencia='atraso',
            faltou=False,
            turno='manha',
            atendido_por='Sonia',
            registrado_por=self.user,
        )

        response = self._post_ocorrencia_registrar('atraso', enviar_reincidencia='sim')

        self.assertContains(response, 'Ja existe um registro para este aluno nesta data com o mesmo tipo de ocorrencia.')
        self.assertEqual(RegistroOcorrenciaAluno.objects.filter(aluno=self.aluno, tipo_ocorrencia='atraso').count(), 1)

    def test_ocorrencia_piercing_rota_real_segunda_vez_nao_duplica(self):
        self.aluno.telefone = '(44) 99999-0000'
        self.aluno.save(update_fields=['telefone'])
        RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 15),
            tipo_ocorrencia='piercing',
            faltou=False,
            turno='manha',
            atendido_por='Sonia',
            registrado_por=self.user,
        )

        response = self._post_ocorrencia_registrar('piercing', enviar_reincidencia='sim')

        self.assertContains(response, 'Ja existe um registro para este aluno nesta data com o mesmo tipo de ocorrencia.')
        self.assertEqual(RegistroOcorrenciaAluno.objects.filter(aluno=self.aluno, tipo_ocorrencia='piercing').count(), 1)

    def test_ocorrencia_atraso_primeira_vez_registra_com_sucesso(self):
        self.aluno.telefone = '(44) 99999-0000'
        self.aluno.save(update_fields=['telefone'])

        response = self._post_ocorrencia_digitador('atraso')

        self.assertTrue(RegistroOcorrenciaAluno.objects.filter(aluno=self.aluno, tipo_ocorrencia='atraso').exists())
        self.assertContains(response, 'incluído no relatório semanal')

    def test_ocorrencia_atraso_com_maiuscula_registra_com_sucesso(self):
        self.aluno.telefone = '(44) 99999-0000'
        self.aluno.save(update_fields=['telefone'])

        response = self._post_ocorrencia_digitador('Atraso')

        self.assertTrue(RegistroOcorrenciaAluno.objects.filter(aluno=self.aluno, tipo_ocorrencia='atraso').exists())
        self.assertContains(response, 'incluído no relatório semanal')

    def test_ocorrencia_atraso_com_espacos_registra_com_sucesso(self):
        self.aluno.telefone = '(44) 99999-0000'
        self.aluno.save(update_fields=['telefone'])

        response = self._post_ocorrencia_digitador(' atraso ')

        self.assertTrue(RegistroOcorrenciaAluno.objects.filter(aluno=self.aluno, tipo_ocorrencia='atraso').exists())
        self.assertContains(response, 'incluído no relatório semanal')

    def test_ocorrencia_atraso_segunda_vez_nao_duplica(self):
        self.aluno.telefone = '(44) 99999-0000'
        self.aluno.save(update_fields=['telefone'])
        RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 15),
            tipo_ocorrencia='atraso',
            faltou=False,
            turno='manha',
            atendido_por='Sonia',
            registrado_por=self.user,
        )

        response = self._post_ocorrencia_digitador('atraso', enviar_reincidencia='sim')

        self.assertContains(response, 'Ja existe um registro para este aluno nesta data com o mesmo tipo de ocorrencia.')
        self.assertEqual(RegistroOcorrenciaAluno.objects.filter(aluno=self.aluno, tipo_ocorrencia='atraso').count(), 1)

    def test_ocorrencia_piercing_digitador_segunda_vez_nao_duplica(self):
        self.aluno.telefone = '(44) 99999-0000'
        self.aluno.save(update_fields=['telefone'])
        RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 15),
            tipo_ocorrencia='piercing',
            faltou=False,
            turno='manha',
            atendido_por='Sonia',
            registrado_por=self.user,
        )

        response = self._post_ocorrencia_digitador('piercing', enviar_reincidencia='sim')

        self.assertContains(response, 'Ja existe um registro para este aluno nesta data com o mesmo tipo de ocorrencia.')
        self.assertEqual(RegistroOcorrenciaAluno.objects.filter(aluno=self.aluno, tipo_ocorrencia='piercing').count(), 1)

    def test_comando_migrar_faltas_ocorrencias_dry_run_nao_grava(self):
        RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 16),
            tipo_ocorrencia='falta',
            faltou=True,
            turno='manha',
            atendido_por='Sonia',
            motivo_alegado='Ausencia informada',
            observacoes_adicionais='Observacao antiga',
            registrado_por=self.user,
        )
        out = StringIO()

        call_command('migrar_faltas_ocorrencias', stdout=out)

        self.assertEqual(RegistroFaltaAluno.objects.count(), 0)
        saida = out.getvalue()
        self.assertIn('Modo: DRY-RUN', saida)
        self.assertIn('Total encontrado: 1', saida)
        self.assertIn('Total que sera migrado: 1', saida)
        self.assertIn('Duplicados ignorados: 0', saida)

    def test_comando_migrar_faltas_ocorrencias_confirma_e_ignora_duplicados(self):
        outro_aluno = Aluno.objects.create(nome='Outro Aluno', numero=8, turma=self.turma)
        RegistroFaltaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 16),
            quantidade_faltas=1,
            pedagoga='Sonia',
            registrado_por=self.user,
        )
        RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 5, 16),
            tipo_ocorrencia='falta',
            faltou=True,
            turno='manha',
            atendido_por='Sonia',
            registrado_por=self.user,
        )
        RegistroOcorrenciaAluno.objects.create(
            aluno=outro_aluno,
            data=date(2026, 5, 17),
            tipo_ocorrencia='Falta',
            faltou=True,
            turno='manha',
            atendido_por='Simone',
            motivo_alegado='Faltou no periodo',
            observacoes_adicionais='Contato pendente',
            registrado_por=self.user,
        )
        out = StringIO()

        call_command('migrar_faltas_ocorrencias', '--confirmar', stdout=out)

        self.assertEqual(RegistroFaltaAluno.objects.count(), 2)
        falta_migrada = RegistroFaltaAluno.objects.get(aluno=outro_aluno)
        self.assertEqual(falta_migrada.data, date(2026, 5, 17))
        self.assertEqual(falta_migrada.pedagoga, 'Simone')
        self.assertEqual(falta_migrada.registrado_por, self.user)
        self.assertIn('Contato pendente', falta_migrada.observacoes)
        self.assertIn('Faltou no periodo', falta_migrada.observacoes)
        self.assertEqual(RegistroOcorrenciaAluno.objects.filter(tipo_ocorrencia__in=['falta', 'Falta']).count(), 2)

        saida = out.getvalue()
        self.assertIn('Total encontrado: 2', saida)
        self.assertIn('Total que sera migrado: 1', saida)
        self.assertIn('Duplicados ignorados: 1', saida)
        self.assertIn('Total migrado: 1', saida)


class WhatsAppServiceTests(TestCase):
    def test_formatar_numero_whatsapp_adiciona_codigo_do_brasil(self):
        self.assertEqual(formatar_numero_whatsapp('(44) 99999-0000'), '5544999990000')

    @override_settings(WHATSAPP_TOKEN='', PHONE_NUMBER_ID='')
    def test_enviar_mensagem_whatsapp_sem_configuracao_retorna_erro_controlado(self):
        resultado = enviar_mensagem_whatsapp('(44) 99999-0000', 'Mensagem de teste')

        self.assertFalse(resultado['status'])
        self.assertEqual(resultado['numero'], '5544999990000')
        self.assertIn('WHATSAPP_TOKEN', resultado['erro'])

    @override_settings(
        WHATSAPP_TOKEN='token-teste',
        PHONE_NUMBER_ID='123456',
        WHATSAPP_API_VERSION='v20.0',
    )
    @patch('apps.services.whatsapp_service.requests.post')
    def test_enviar_template_aviso_falta_aluno_usa_template_aprovado(self, post_mock):
        post_mock.return_value.ok = True
        post_mock.return_value.status_code = 200
        post_mock.return_value.json.return_value = {'messages': [{'id': 'wamid.teste'}]}

        resultado = enviar_template_aviso_falta_aluno('44999990000', 'Aluno Teste', '15/05/2026')

        self.assertTrue(resultado['status'])
        payload = post_mock.call_args.kwargs['json']
        self.assertEqual(payload['type'], 'template')
        self.assertEqual(payload['template']['name'], 'aviso_falta_aluno')
        self.assertEqual(payload['template']['language']['code'], 'pt_BR')
        parametros = payload['template']['components'][0]['parameters']
        self.assertEqual(parametros[0]['text'], 'Aluno Teste')
        self.assertEqual(parametros[1]['text'], '15/05/2026')

    @override_settings(
        WHATSAPP_TOKEN='token-teste',
        PHONE_NUMBER_ID='123456',
        WHATSAPP_API_VERSION='v20.0',
    )
    @patch('apps.services.whatsapp_service.requests.post')
    def test_enviar_template_aviso_ocorrencia_aluno_usa_template_aprovado(self, post_mock):
        post_mock.return_value.ok = True
        post_mock.return_value.status_code = 200
        post_mock.return_value.json.return_value = {'messages': [{'id': 'wamid.teste'}]}

        resultado = enviar_template_aviso_ocorrencia_aluno(
            '44999990000',
            'Aluno Teste',
            'Uso de Piercing',
            '15/05/2026',
        )

        self.assertTrue(resultado['status'])
        payload = post_mock.call_args.kwargs['json']
        self.assertEqual(payload['type'], 'template')
        self.assertEqual(payload['template']['name'], 'aviso_ocorrencia_aluno')
        self.assertEqual(payload['template']['language']['code'], 'pt_BR')
        parametros = payload['template']['components'][0]['parameters']
        self.assertEqual(parametros[0]['text'], 'Aluno Teste')
        self.assertEqual(parametros[1]['text'], 'Uso de Piercing')
        self.assertEqual(parametros[2]['text'], '15/05/2026')


class CronogramaLaboratoriosTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='diretivo', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.user)
        self.turma = Turma.objects.create(nome='2A', ano=2026, serie='2 Ano', ativa=True, turno='manha')
        self.professor_manha = Professor.objects.create(nome_completo='Ana Maria Silva', ativo=True)
        self.professor_tarde = Professor.objects.create(nome_completo='Bruno Costa Lima', ativo=True)
        self.labs = [
            Laboratorio.objects.create(nome=f'Laboratorio {i}', tipo='fixo', equipamento='Computadores', ativo=True)
            for i in range(1, 6)
        ]
        self.data_segunda = date(2026, 5, 18)
        AgendamentoLab.objects.create(
            laboratorio=self.labs[0],
            data=self.data_segunda,
            horario='1',
            turno='manha',
            professor=self.professor_manha,
            turma=self.turma,
            disciplina='Matematica',
            observacao='Levar projetor',
            registrado_por=self.user,
        )
        AgendamentoLab.objects.create(
            laboratorio=self.labs[1],
            data=self.data_segunda,
            horario='2',
            turno='tarde',
            professor=self.professor_tarde,
            turma=self.turma,
            disciplina='Historia',
            registrado_por=self.user,
        )

    def _get_cronograma(self, **params):
        base = {'data_inicio': self.data_segunda.isoformat()}
        base.update(params)
        turno = base.pop('turno', 'manha')
        return self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': turno}), base,
        )

    def test_cronograma_filtra_por_laboratorio(self):
        response = self._get_cronograma(laboratorio=str(self.labs[0].id))

        self.assertEqual(list(response.context['laboratorios']), [self.labs[0]])
        self.assertContains(response, 'Matematica')
        self.assertNotContains(response, 'Historia')

    def test_cronograma_filtra_turno_manha(self):
        response = self._get_cronograma(turno='manha')

        self.assertContains(response, 'Matematica')
        self.assertNotContains(response, 'Historia')
        self.assertNotContains(response, 'data-turno-separator="manha"')
        self.assertNotContains(response, 'data-turno-separator="tarde"')
        self.assertTrue(all(slot['turno'] == 'manha' for slot in response.context['slots_cronograma']))

    def test_cronograma_filtra_turno_tarde(self):
        response = self._get_cronograma(turno='tarde')

        self.assertContains(response, 'Historia')
        self.assertNotContains(response, 'Matematica')
        self.assertNotContains(response, 'data-turno-separator="manha"')
        self.assertNotContains(response, 'data-turno-separator="tarde"')
        self.assertTrue(all(slot['turno'] == 'tarde' for slot in response.context['slots_cronograma']))

    def test_cronograma_filtra_laboratorio_e_turno(self):
        response = self._get_cronograma(laboratorio=str(self.labs[1].id), turno='tarde')

        self.assertEqual(list(response.context['laboratorios']), [self.labs[1]])
        self.assertContains(response, 'Historia')
        self.assertNotContains(response, 'Matematica')

    def test_cronograma_todos_laboratorios(self):
        response = self._get_cronograma()

        self.assertEqual(len(response.context['laboratorios']), 5)
        for lab in self.labs:
            self.assertContains(response, lab.nome)

    def test_cronograma_todos_turnos(self):
        response_manha = self._get_cronograma(turno='manha')
        response_tarde = self._get_cronograma(turno='tarde')

        self.assertTrue(all(slot['turno'] == 'manha' for slot in response_manha.context['slots_cronograma']))
        self.assertTrue(all(slot['turno'] == 'tarde' for slot in response_tarde.context['slots_cronograma']))
        self.assertContains(response_manha, 'Matematica')
        self.assertContains(response_tarde, 'Historia')

    def test_cronograma_nao_mostra_botoes_de_navegacao_semanal(self):
        response = self._get_cronograma()

        self.assertNotContains(response, 'Semana Anterior')
        self.assertNotContains(response, 'Proxima Semana')

    def test_cronograma_sem_data_inicio_usa_semana_mais_recente_com_agendamentos(self):
        data_recente = date(2026, 6, 5)
        AgendamentoLab.objects.create(
            laboratorio=self.labs[2],
            data=data_recente,
            horario='3',
            turno='manha',
            professor=self.professor_manha,
            turma=self.turma,
            disciplina='Robotica',
            registrado_por=self.user,
        )

        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}),
        )

        self.assertEqual(response.context['data_inicio'], date(2026, 6, 1))
        self.assertContains(response, 'Robotica')
        self.assertNotContains(response, 'Matematica')

    def test_cronograma_sem_agendamentos_usa_semana_atual(self):
        AgendamentoLab.objects.all().delete()
        hoje = date.today()

        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}),
        )

        self.assertEqual(response.context['data_inicio'], hoje - timedelta(days=hoje.weekday()))

    def test_cronograma_sem_data_inicio_respeita_filtro_laboratorio(self):
        AgendamentoLab.objects.create(
            laboratorio=self.labs[0],
            data=date(2026, 5, 20),
            horario='3',
            turno='manha',
            professor=self.professor_manha,
            turma=self.turma,
            disciplina='Quimica',
            registrado_por=self.user,
        )
        AgendamentoLab.objects.create(
            laboratorio=self.labs[1],
            data=date(2026, 6, 5),
            horario='4',
            turno='tarde',
            professor=self.professor_tarde,
            turma=self.turma,
            disciplina='Robotica',
            registrado_por=self.user,
        )

        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}),
            {'laboratorio': str(self.labs[0].id)},
        )

        self.assertEqual(response.context['data_inicio'], date(2026, 5, 18))
        self.assertContains(response, 'Quimica')
        self.assertNotContains(response, 'Robotica')

    def test_cronograma_sem_data_inicio_respeita_filtro_turno(self):
        AgendamentoLab.objects.create(
            laboratorio=self.labs[2],
            data=date(2026, 5, 20),
            horario='3',
            turno='manha',
            professor=self.professor_manha,
            turma=self.turma,
            disciplina='Quimica',
            registrado_por=self.user,
        )
        AgendamentoLab.objects.create(
            laboratorio=self.labs[3],
            data=date(2026, 6, 5),
            horario='4',
            turno='tarde',
            professor=self.professor_tarde,
            turma=self.turma,
            disciplina='Robotica',
            registrado_por=self.user,
        )

        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}),
        )

        self.assertEqual(response.context['data_inicio'], date(2026, 5, 18))
        self.assertContains(response, 'Quimica')
        self.assertNotContains(response, 'Robotica')

    def test_cronograma_professores_aparecem_no_dia_correto(self):
        response_manha = self._get_cronograma(turno='manha')
        response_tarde = self._get_cronograma(turno='tarde')

        self.assertContains(response_manha, 'Segunda-feira')
        self.assertContains(response_manha, self.professor_manha.nome_abreviado)
        self.assertContains(response_tarde, self.professor_tarde.nome_abreviado)
        self.assertContains(response_manha, 'Levar projetor')


class ExcluirSemanaCronogramaLaboratorioTests(TestCase):
    """Fase 2 complementar (correcao de seguranca): a rota antiga
    excluir_semana_cronograma_laboratorio nao esta mais linkada em nenhuma
    interface, mas continuava acessivel por URL/POST direto e fazia exclusao
    fisica de AgendamentoLab sem passar por SemanaCronogramaLaboratorio - um
    bypass real da protecao de escrita introduzida na Fase 2. Agora e um
    no-op seguro: nunca apaga nenhum AgendamentoLab, para nenhum status
    (ativa/editando/historica/cancelada) nem para turnos que ainda nao
    adotaram a tabela nova (compatibilidade legada)."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='diretivo-exclusao', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.user)
        self.turma = Turma.objects.create(nome='2A', ano=2026, serie='2 Ano', ativa=True, turno='manha')
        self.professor_manha = Professor.objects.create(nome_completo='Ana Maria Silva', ativo=True)
        self.professor_tarde = Professor.objects.create(nome_completo='Bruno Costa Lima', ativo=True)
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.semana_inicio = date(2026, 5, 18)
        self.semana_ativa_manha = date(2026, 6, 1)
        self.semana_ativa_tarde = date(2026, 6, 8)
        # Periodos ativos legados apontando para semanas diferentes de
        # self.semana_inicio, para separar os testes de "nao apaga nada em
        # geral" dos testes especificos de protecao da semana ativa.
        self.configuracao = ConfiguracaoCronogramaLaboratorio.objects.create(
            data_inicio_cronograma_ativo=self.semana_ativa_manha,
            data_fim_cronograma_ativo=self.semana_ativa_manha + timedelta(days=6),
            data_inicio_manha_ativo=self.semana_ativa_manha,
            data_fim_manha_ativo=self.semana_ativa_manha + timedelta(days=6),
            data_inicio_tarde_ativo=self.semana_ativa_tarde,
            data_fim_tarde_ativo=self.semana_ativa_tarde + timedelta(days=6),
        )

    def _agendar(self, turno, data, horario='1', professor=None):
        return AgendamentoLab.objects.create(
            laboratorio=self.laboratorio,
            data=data,
            horario=horario,
            turno=turno,
            professor=professor or (self.professor_manha if turno == 'manha' else self.professor_tarde),
            turma=self.turma,
            disciplina='Matematica',
            registrado_por=self.user,
        )

    def _excluir(self, turno, data_inicio, follow=False):
        return self.client.post(
            reverse('excluir_semana_cronograma_laboratorio'),
            {'turno': turno, 'data_inicio': data_inicio.isoformat()},
            follow=follow,
        )

    def _criar_semana_nova(self, turno, data_inicio, status):
        return SemanaCronogramaLaboratorio.objects.create(
            turno=turno, data_inicio=data_inicio, status=status,
        )

    # --------------------------------------------- compatibilidade legada

    def test_excluir_na_manha_legado_nunca_apaga_agendamentos(self):
        agendamento = self._agendar('manha', self.semana_inicio)

        response = self._excluir('manha', self.semana_inicio)

        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))
        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())

    def test_excluir_na_tarde_legado_nunca_apaga_agendamentos(self):
        agendamento = self._agendar('tarde', self.semana_inicio)

        response = self._excluir('tarde', self.semana_inicio)

        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))
        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())

    def test_excluir_manha_nao_afeta_tarde_e_nao_apaga_nada(self):
        agendamento_manha = self._agendar('manha', self.semana_inicio, horario='1')
        agendamento_tarde = self._agendar('tarde', self.semana_inicio, horario='2')

        self._excluir('manha', self.semana_inicio)

        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento_manha.id).exists())
        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento_tarde.id).exists())

    def test_excluir_tarde_nao_afeta_manha_e_nao_apaga_nada(self):
        agendamento_manha = self._agendar('manha', self.semana_inicio, horario='1')
        agendamento_tarde = self._agendar('tarde', self.semana_inicio, horario='2')

        self._excluir('tarde', self.semana_inicio)

        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento_manha.id).exists())
        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento_tarde.id).exists())

    def test_nao_permite_excluir_periodo_ativo_legado(self):
        agendamento = self._agendar('manha', self.semana_inicio)
        self.configuracao.data_inicio_manha_ativo = self.semana_inicio
        self.configuracao.data_fim_manha_ativo = self.semana_inicio + timedelta(days=6)
        self.configuracao.save()

        response = self._excluir('manha', self.semana_inicio, follow=True)

        mensagens = [str(m) for m in response.context['messages']]
        self.assertTrue(any('ativa' in m.lower() for m in mensagens))
        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())

    def test_nenhum_turno_e_afetado_mesmo_quando_apenas_um_esta_ativo_legado(self):
        # Antes desta correcao de seguranca, uma semana FORA do periodo
        # ativo era excluida "normalmente" por esta rota. Isso nao existe
        # mais: nenhuma chamada a esta rota apaga qualquer AgendamentoLab,
        # esteja a semana ativa, editando, historica, cancelada ou nem
        # cadastrada em SemanaCronogramaLaboratorio.
        agendamento_manha = self._agendar('manha', self.semana_inicio)
        agendamento_tarde = self._agendar('tarde', self.semana_inicio)
        self.configuracao.data_inicio_tarde_ativo = self.semana_inicio
        self.configuracao.data_fim_tarde_ativo = self.semana_inicio + timedelta(days=6)
        self.configuracao.save()

        self._excluir('tarde', self.semana_inicio)
        self.assertTrue(
            AgendamentoLab.objects.filter(id=agendamento_tarde.id).exists(),
            'A semana ativa da tarde nao deveria ter sido excluida.',
        )

        self._excluir('manha', self.semana_inicio)
        self.assertTrue(
            AgendamentoLab.objects.filter(id=agendamento_manha.id).exists(),
            'A exclusao fisica foi desativada - nenhuma semana deveria ser excluida, ativa ou nao.',
        )

    def test_nao_exclui_agendamentos_de_outra_semana_nem_da_semana_pedida(self):
        outra_semana = self.semana_inicio + timedelta(days=7)
        agendamento_semana_atual = self._agendar('manha', self.semana_inicio)
        agendamento_outra_semana = self._agendar('manha', outra_semana)

        self._excluir('manha', self.semana_inicio)

        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento_semana_atual.id).exists())
        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento_outra_semana.id).exists())

    def test_get_nao_exclui_nenhum_agendamento(self):
        agendamento_manha = self._agendar('manha', self.semana_inicio)
        agendamento_tarde = self._agendar('tarde', self.semana_inicio)

        response = self.client.get(
            reverse('excluir_semana_cronograma_laboratorio'),
            {'turno': 'manha', 'data_inicio': self.semana_inicio.isoformat()},
        )

        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))
        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento_manha.id).exists())
        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento_tarde.id).exists())

    # ------------------------------- Fase 2 complementar: por status na nova tabela

    def test_url_direta_em_semana_ativa_nao_apaga_agendamentos(self):
        semana = self._criar_semana_nova('manha', self.semana_inicio, 'ativa')
        agendamento = self._agendar('manha', self.semana_inicio)
        agendamento.semana = semana
        agendamento.save(update_fields=['semana'])

        response = self._excluir('manha', self.semana_inicio, follow=True)

        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())
        mensagens = [str(m) for m in response.context['messages']]
        self.assertTrue(any('ativa' in m.lower() for m in mensagens))

    def test_url_direta_em_semana_editando_nao_apaga_agendamentos_e_indica_cancelar(self):
        semana = self._criar_semana_nova('manha', self.semana_inicio, 'editando')
        agendamento = self._agendar('manha', self.semana_inicio)
        agendamento.semana = semana
        agendamento.save(update_fields=['semana'])

        response = self._excluir('manha', self.semana_inicio, follow=True)

        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())
        mensagens = [str(m) for m in response.context['messages']]
        self.assertTrue(any('cancelar' in m.lower() for m in mensagens))

    def test_url_direta_em_semana_historica_nao_apaga_agendamentos(self):
        semana = self._criar_semana_nova('manha', self.semana_inicio, 'historica')
        agendamento = self._agendar('manha', self.semana_inicio)
        agendamento.semana = semana
        agendamento.save(update_fields=['semana'])

        response = self._excluir('manha', self.semana_inicio, follow=True)

        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())
        mensagens = [str(m) for m in response.context['messages']]
        self.assertTrue(any('histor' in m.lower() for m in mensagens))

    def test_url_direta_em_semana_cancelada_nao_apaga_agendamentos(self):
        semana = self._criar_semana_nova('manha', self.semana_inicio, 'cancelada')
        agendamento = self._agendar('manha', self.semana_inicio)
        agendamento.semana = semana
        agendamento.save(update_fields=['semana'])

        response = self._excluir('manha', self.semana_inicio, follow=True)

        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())
        mensagens = [str(m) for m in response.context['messages']]
        self.assertTrue(any('cancelada' in m.lower() for m in mensagens))

    def test_url_direta_nao_apaga_mesmo_sem_nenhuma_semana_cadastrada(self):
        """Turno 'tarde' aqui nunca teve nenhuma linha criada em
        SemanaCronogramaLaboratorio (nem por este teste, nem pelo setUp) -
        cobre o caso de turno totalmente 'nao adotado' pedindo uma data que
        tambem nao e a ativa legada."""
        agendamento = self._agendar('tarde', self.semana_inicio)

        response = self._excluir('tarde', self.semana_inicio, follow=True)

        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())
        mensagens = [str(m) for m in response.context['messages']]
        self.assertTrue(any('desativada' in m.lower() for m in mensagens))


class EditarSemanaCronogramaLaboratorioTests(TestCase):
    """Cobre o fluxo Editar (preparar semana) vs Ativar (publicar semana) por turno."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='diretivo-editar', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.user)
        self.turma = Turma.objects.create(nome='2A', ano=2026, serie='2 Ano', ativa=True, turno='manha')
        self.professor_manha = Professor.objects.create(nome_completo='Ana Maria Silva', ativo=True)
        self.professor_tarde = Professor.objects.create(nome_completo='Bruno Costa Lima', ativo=True)
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.semana_inicio = date(2026, 5, 18)

    def _agendar(self, turno, data, horario='1', professor=None):
        return AgendamentoLab.objects.create(
            laboratorio=self.laboratorio,
            data=data,
            horario=horario,
            turno=turno,
            professor=professor or (self.professor_manha if turno == 'manha' else self.professor_tarde),
            turma=self.turma,
            disciplina='Matematica',
            registrado_por=self.user,
        )

    def _editar(self, turno, data_inicio):
        return self.client.post(
            reverse('editar_semana_cronograma_laboratorio'),
            {'turno': turno, 'data_inicio': data_inicio.isoformat()},
        )

    def _ativar(self, turno, data_inicio):
        return self.client.post(
            reverse('ativar_cronograma_laboratorio'),
            {'turno': turno, 'data_inicio': data_inicio.isoformat()},
        )

    def _excluir(self, turno, data_inicio):
        return self.client.post(
            reverse('excluir_semana_cronograma_laboratorio'),
            {'turno': turno, 'data_inicio': data_inicio.isoformat()},
        )

    def _diagnostico(self):
        return self.client.get(reverse('diagnostico_cronograma_laboratorios'))

    def test_escolher_semana_para_edicao_nao_ativa_a_semana(self):
        response = self._editar('manha', self.semana_inicio)

        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))
        ativo_inicio, _ = obter_cronograma_ativo('manha')
        self.assertNotEqual(ativo_inicio, self.semana_inicio)
        # Fase 2: a semana em edicao agora e uma linha propria em
        # SemanaCronogramaLaboratorio (estado compartilhado, nao mais preso a
        # sessao) - "Criar/Preparar semana" nao cria mais uma linha de
        # ConfiguracaoCronogramaLaboratorio como efeito colateral (esse campo
        # antigo so e escrito nas acoes de ativar/desativar, como
        # compatibilidade temporaria - ver relatorio da Fase 2, secao 8).
        self.assertTrue(
            SemanaCronogramaLaboratorio.objects.filter(
                turno='manha', data_inicio=self.semana_inicio, status='editando',
            ).exists()
        )

        contexto = self._diagnostico().context
        self.assertEqual(contexto['semana_editando_manha_inicio'], self.semana_inicio)
        self.assertIsNone(contexto['semana_ativa_manha_inicio'])

    def test_editar_manha_nao_altera_tarde(self):
        self._editar('manha', self.semana_inicio)

        contexto = self._diagnostico().context
        self.assertEqual(contexto['semana_editando_manha_inicio'], self.semana_inicio)
        self.assertIsNone(contexto['semana_editando_tarde_inicio'])
        self.assertIsNone(contexto['semana_ativa_tarde_inicio'])

    def test_editar_tarde_nao_altera_manha(self):
        self._editar('tarde', self.semana_inicio)

        contexto = self._diagnostico().context
        self.assertEqual(contexto['semana_editando_tarde_inicio'], self.semana_inicio)
        self.assertIsNone(contexto['semana_editando_manha_inicio'])
        self.assertIsNone(contexto['semana_ativa_manha_inicio'])

    def test_somente_acao_de_ativar_altera_o_periodo_ativo(self):
        self._agendar('manha', self.semana_inicio)

        self._editar('manha', self.semana_inicio)
        self.assertTrue(
            SemanaCronogramaLaboratorio.objects.filter(
                turno='manha', data_inicio=self.semana_inicio, status='editando',
            ).exists()
        )
        ativo_inicio, _ = obter_cronograma_ativo('manha')
        self.assertNotEqual(ativo_inicio, self.semana_inicio)

        self._ativar('manha', self.semana_inicio)
        ativo_inicio, ativo_fim = obter_cronograma_ativo('manha')
        self.assertEqual(ativo_inicio, self.semana_inicio)
        self.assertEqual(ativo_fim, self.semana_inicio + timedelta(days=6))

    def test_ativar_manha_nao_altera_tarde(self):
        self._agendar('manha', self.semana_inicio)
        outra_semana_tarde = self.semana_inicio + timedelta(days=7)
        self._agendar('tarde', outra_semana_tarde)
        self._ativar('tarde', outra_semana_tarde)
        tarde_antes_inicio, tarde_antes_fim = obter_cronograma_ativo('tarde')

        self._ativar('manha', self.semana_inicio)

        tarde_depois_inicio, tarde_depois_fim = obter_cronograma_ativo('tarde')
        self.assertEqual(tarde_antes_inicio, tarde_depois_inicio)
        self.assertEqual(tarde_antes_fim, tarde_depois_fim)

    def test_ativar_tarde_nao_altera_manha(self):
        self._agendar('tarde', self.semana_inicio)
        outra_semana_manha = self.semana_inicio + timedelta(days=7)
        self._agendar('manha', outra_semana_manha)
        self._ativar('manha', outra_semana_manha)
        manha_antes_inicio, manha_antes_fim = obter_cronograma_ativo('manha')

        self._ativar('tarde', self.semana_inicio)

        manha_depois_inicio, manha_depois_fim = obter_cronograma_ativo('manha')
        self.assertEqual(manha_antes_inicio, manha_depois_inicio)
        self.assertEqual(manha_antes_fim, manha_depois_fim)

    def test_semana_editada_pode_ser_preparada_antes_de_ativar(self):
        self._agendar('manha', self.semana_inicio)

        response_editar = self._editar('manha', self.semana_inicio)
        self.assertRedirects(response_editar, reverse('diagnostico_cronograma_laboratorios'))
        contexto = self._diagnostico().context
        self.assertEqual(contexto['semana_editando_manha_inicio'], self.semana_inicio)
        self.assertIsNone(contexto['semana_ativa_manha_inicio'])

        response_ativar = self._ativar('manha', self.semana_inicio)
        self.assertRedirects(response_ativar, reverse('diagnostico_cronograma_laboratorios'))

        ativo_inicio, ativo_fim = obter_cronograma_ativo('manha')
        self.assertEqual(ativo_inicio, self.semana_inicio)
        self.assertEqual(ativo_fim, self.semana_inicio + timedelta(days=6))

    def test_semana_ativa_continua_protegida_contra_exclusao_apos_ativar(self):
        agendamento = self._agendar('manha', self.semana_inicio)
        self._editar('manha', self.semana_inicio)
        self._ativar('manha', self.semana_inicio)

        response = self.client.post(
            reverse('excluir_semana_cronograma_laboratorio'),
            {'turno': 'manha', 'data_inicio': self.semana_inicio.isoformat()},
            follow=True,
        )

        mensagens = [str(m) for m in response.context['messages']]
        self.assertTrue(any('ativa' in m.lower() for m in mensagens))
        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())


class SemanaEmEdicaoCompartilhadaEntreAdministradoresTests(TestCase):
    """Cobre a correcao do problema de auditoria: a semana em edicao
    (preparacao) e gravada em ConfiguracaoCronogramaLaboratorio - estado
    compartilhado entre toda a equipe diretiva - e nao mais presa a sessao
    de um unico usuario. Usa dois administradores com clients/sessoes
    totalmente distintos para provar que o estado e visivel e continuavel
    por qualquer um deles, com manha e tarde permanecendo isoladas."""

    def setUp(self):
        self.admin_a = User.objects.create_user(
            username='admin-a-compartilhado', password='senha-teste', is_superuser=True,
        )
        self.admin_b = User.objects.create_user(
            username='admin-b-compartilhado', password='senha-teste', is_superuser=True,
        )
        self.cliente_a = Client()
        self.cliente_b = Client()
        self.cliente_a.force_login(self.admin_a)
        self.cliente_b.force_login(self.admin_b)

        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.turma_manha = Turma.objects.create(nome='1A', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.turma_tarde = Turma.objects.create(nome='6A', ano=2026, serie='6 Ano', ativa=True, turno='tarde')
        self.professor = Professor.objects.create(nome_completo='Elaine Vitoria Bomfim', ativo=True)

    def _editar(self, cliente, turno, data_inicio):
        return cliente.post(
            reverse('editar_semana_cronograma_laboratorio'),
            {'turno': turno, 'data_inicio': data_inicio.isoformat()},
        )

    def _cancelar(self, cliente, turno):
        return cliente.post(reverse('cancelar_edicao_semana_cronograma_laboratorio'), {'turno': turno})

    def _diagnostico(self, cliente):
        return cliente.get(reverse('diagnostico_cronograma_laboratorios'))

    def test_admin_a_prepara_manha_e_admin_b_enxerga_a_mesma_semana(self):
        semana = date(2026, 9, 7)
        self._editar(self.cliente_a, 'manha', semana)

        contexto_b = self._diagnostico(self.cliente_b).context
        self.assertEqual(contexto_b['semana_editando_manha_inicio'], semana)

    def test_admin_a_prepara_tarde_e_admin_b_enxerga_a_mesma_semana(self):
        semana = date(2026, 9, 14)
        self._editar(self.cliente_a, 'tarde', semana)

        contexto_b = self._diagnostico(self.cliente_b).context
        self.assertEqual(contexto_b['semana_editando_tarde_inicio'], semana)

    def test_admin_b_continua_a_edicao_iniciada_por_admin_a(self):
        """Admin B consegue de fato editar (criar um agendamento real) na
        mesma semana que Admin A comecou a preparar, via a tela travada
        (Editar manha)."""
        semana = date(2026, 9, 7)
        self._editar(self.cliente_a, 'manha', semana)

        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        self.cliente_b.post(url, {
            'laboratorio_id': self.laboratorio.id,
            'data': semana.isoformat(),
            'horario': '1',
            'turno': 'manha',
            'professor': self.professor.id,
            'turma': self.turma_manha.id,
            'disciplina': 'Continuado por B',
            'observacao': '',
            'data_inicio': semana.isoformat(),
            'laboratorio_filtro': '',
            'scroll_y': '',
        })
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Continuado por B', turno='manha').exists())

    def test_admin_b_cancela_edicao_da_manha_e_admin_a_tambem_deixa_de_ver(self):
        semana = date(2026, 9, 7)
        self._editar(self.cliente_a, 'manha', semana)

        self._cancelar(self.cliente_b, 'manha')

        contexto_a = self._diagnostico(self.cliente_a).context
        self.assertIsNone(contexto_a['semana_editando_manha_inicio'])

    def test_admin_b_cancela_edicao_da_tarde_e_admin_a_tambem_deixa_de_ver(self):
        semana = date(2026, 9, 14)
        self._editar(self.cliente_a, 'tarde', semana)

        self._cancelar(self.cliente_b, 'tarde')

        contexto_a = self._diagnostico(self.cliente_a).context
        self.assertIsNone(contexto_a['semana_editando_tarde_inicio'])

    def test_cancelar_manha_nao_altera_tarde(self):
        semana_manha = date(2026, 9, 7)
        semana_tarde = date(2026, 9, 14)
        self._editar(self.cliente_a, 'manha', semana_manha)
        self._editar(self.cliente_a, 'tarde', semana_tarde)

        self._cancelar(self.cliente_b, 'manha')

        contexto = self._diagnostico(self.cliente_a).context
        self.assertIsNone(contexto['semana_editando_manha_inicio'])
        self.assertEqual(contexto['semana_editando_tarde_inicio'], semana_tarde)

    def test_cancelar_tarde_nao_altera_manha(self):
        semana_manha = date(2026, 9, 7)
        semana_tarde = date(2026, 9, 14)
        self._editar(self.cliente_a, 'manha', semana_manha)
        self._editar(self.cliente_a, 'tarde', semana_tarde)

        self._cancelar(self.cliente_b, 'tarde')

        contexto = self._diagnostico(self.cliente_a).context
        self.assertEqual(contexto['semana_editando_manha_inicio'], semana_manha)
        self.assertIsNone(contexto['semana_editando_tarde_inicio'])

    def test_semana_ativa_e_semana_em_edicao_coexistem(self):
        semana_ativa = date(2026, 8, 24)
        semana_editando = date(2026, 8, 31)
        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=semana_ativa, horario='1', turno='manha',
            professor=self.professor, turma=self.turma_manha, disciplina='Aula ativa',
            registrado_por=self.admin_a,
        )
        self.cliente_a.post(
            reverse('ativar_cronograma_laboratorio'),
            {'turno': 'manha', 'data_inicio': semana_ativa.isoformat()},
        )

        self._editar(self.cliente_a, 'manha', semana_editando)

        contexto = self._diagnostico(self.cliente_b).context
        self.assertEqual(contexto['semana_ativa_manha_inicio'], semana_ativa)
        self.assertEqual(contexto['semana_editando_manha_inicio'], semana_editando)
        self.assertNotEqual(contexto['semana_ativa_manha_inicio'], contexto['semana_editando_manha_inicio'])

    def test_fechar_sessao_nao_elimina_o_estado_compartilhado(self):
        """Simula 'trocar de navegador/dispositivo': um client totalmente
        novo, sem nenhum cookie em comum com cliente_a, ainda enxerga a
        semana em edicao - prova de que o estado nao vive mais na sessao."""
        semana = date(2026, 9, 7)
        self._editar(self.cliente_a, 'manha', semana)

        novo_admin = User.objects.create_user(
            username='admin-novo-dispositivo', password='senha-teste', is_superuser=True,
        )
        novo_cliente = Client()
        novo_cliente.force_login(novo_admin)

        contexto = self._diagnostico(novo_cliente).context
        self.assertEqual(contexto['semana_editando_manha_inicio'], semana)

    def test_manha_e_tarde_continuam_isoladas_entre_administradores_diferentes(self):
        semana_manha = date(2026, 9, 7)
        semana_tarde = date(2026, 9, 14)
        self._editar(self.cliente_a, 'manha', semana_manha)
        self._editar(self.cliente_b, 'tarde', semana_tarde)

        contexto = self._diagnostico(self.cliente_a).context
        self.assertEqual(contexto['semana_editando_manha_inicio'], semana_manha)
        self.assertEqual(contexto['semana_editando_tarde_inicio'], semana_tarde)

    def test_turno_todos_continua_indisponivel(self):
        response = self.cliente_a.get('/laboratorios/cronograma/todos/')
        self.assertEqual(response.status_code, 404)

    def test_agendamentos_existentes_permanecem_intactos(self):
        semana = date(2026, 9, 7)
        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=semana, horario='1', turno='manha',
            professor=self.professor, turma=self.turma_manha, disciplina='Preexistente',
            registrado_por=self.admin_a,
        )
        total_antes = AgendamentoLab.objects.count()

        self._editar(self.cliente_a, 'manha', semana)
        self._editar(self.cliente_b, 'tarde', semana)
        self._cancelar(self.cliente_a, 'manha')

        self.assertEqual(AgendamentoLab.objects.count(), total_antes)
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Preexistente').exists())


class CartaoDiagnosticoExibeAtivoEEmEdicaoTests(TestCase):
    """Cobre a correcao visual: o cartao do diagnostico precisa mostrar
    'ATIVA' e 'EM EDICAO' como dois estados separados e simultaneos por
    turno, com o link de 'em edicao' levando direto para a rota travada do
    turno correto (nunca a rota generica nem turno=todos). Usa dois
    administradores/sessoes diferentes para confirmar que o estado exibido
    e realmente compartilhado (nao lido de sessao)."""

    def setUp(self):
        self.admin_a = User.objects.create_user(
            username='admin-a-cartao', password='senha-teste', is_superuser=True,
        )
        self.admin_b = User.objects.create_user(
            username='admin-b-cartao', password='senha-teste', is_superuser=True,
        )
        self.cliente_a = Client()
        self.cliente_b = Client()
        self.cliente_a.force_login(self.admin_a)
        self.cliente_b.force_login(self.admin_b)

        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.turma_manha = Turma.objects.create(nome='1A', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.turma_tarde = Turma.objects.create(nome='6A', ano=2026, serie='6 Ano', ativa=True, turno='tarde')
        self.professor = Professor.objects.create(nome_completo='Fabio Henrique Alencar', ativo=True)

    def _editar(self, cliente, turno, data_inicio):
        return cliente.post(
            reverse('editar_semana_cronograma_laboratorio'),
            {'turno': turno, 'data_inicio': data_inicio.isoformat()},
        )

    def _diagnostico(self, cliente):
        return cliente.get(reverse('diagnostico_cronograma_laboratorios'))

    def _link_editando(self, turno, data_inicio):
        url_base = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': turno})
        return f'{url_base}?data_inicio={data_inicio.isoformat()}'

    def test_semana_em_edicao_da_manha_aparece_no_cartao_para_admin_a(self):
        semana = date(2026, 9, 7)
        self._editar(self.cliente_a, 'manha', semana)

        response = self._diagnostico(self.cliente_a)
        self.assertContains(response, '📝 Em edição')
        self.assertContains(response, f'desde {semana.strftime("%d/%m/%Y")}')
        self.assertContains(response, self._link_editando('manha', semana))

    def test_semana_em_edicao_da_manha_aparece_tambem_para_admin_b(self):
        semana = date(2026, 9, 7)
        self._editar(self.cliente_a, 'manha', semana)

        response = self._diagnostico(self.cliente_b)
        self.assertContains(response, '📝 Em edição')
        self.assertContains(response, self._link_editando('manha', semana))

    def test_semana_em_edicao_da_tarde_aparece_para_ambos_administradores(self):
        semana = date(2026, 9, 14)
        self._editar(self.cliente_a, 'tarde', semana)

        for cliente in (self.cliente_a, self.cliente_b):
            response = self._diagnostico(cliente)
            self.assertContains(response, '📝 Em edição')
            self.assertContains(response, self._link_editando('tarde', semana))

    def test_ativo_e_em_edicao_aparecem_simultaneamente(self):
        semana_ativa = date(2026, 8, 24)
        semana_editando = date(2026, 8, 31)
        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=semana_ativa, horario='1', turno='manha',
            professor=self.professor, turma=self.turma_manha, disciplina='Aula ativa',
            registrado_por=self.admin_a,
        )
        self.cliente_a.post(
            reverse('ativar_cronograma_laboratorio'),
            {'turno': 'manha', 'data_inicio': semana_ativa.isoformat()},
        )
        self._editar(self.cliente_a, 'manha', semana_editando)

        response = self._diagnostico(self.cliente_b)
        conteudo = response.content.decode()

        self.assertIn(f'desde {semana_ativa.strftime("%d/%m/%Y")}', conteudo)
        self.assertIn('📝 Em edição', conteudo)
        self.assertIn(f'desde {semana_editando.strftime("%d/%m/%Y")}', conteudo)
        self.assertIn(self._link_editando('manha', semana_editando), conteudo)

    def test_manha_e_tarde_permanecem_independentes_no_cartao(self):
        semana_manha = date(2026, 9, 7)
        self._editar(self.cliente_a, 'manha', semana_manha)

        response = self._diagnostico(self.cliente_b)
        conteudo = response.content.decode()

        self.assertIn(self._link_editando('manha', semana_manha), conteudo)
        self.assertNotIn(self._link_editando('tarde', semana_manha), conteudo)
        # so uma ocorrencia do rotulo "Em edicao" - so a manha esta em preparacao
        self.assertEqual(conteudo.count('📝 Em edição'), 1)

    def test_sem_semana_em_edicao_nao_mostra_em_edicao(self):
        response = self._diagnostico(self.cliente_a)
        self.assertNotContains(response, '📝 Em edição')

    def test_link_da_manha_abre_exclusivamente_editar_manha(self):
        semana = date(2026, 9, 7)
        self._editar(self.cliente_a, 'manha', semana)

        response = self._diagnostico(self.cliente_a)
        conteudo = response.content.decode()

        link_manha = self._link_editando('manha', semana)
        self.assertIn(link_manha, conteudo)
        self.assertIn('/laboratorios/cronograma/manha/', link_manha)
        self.assertNotIn('/laboratorios/cronograma/tarde/', link_manha)

        resposta_link = self.cliente_a.get(link_manha)
        self.assertEqual(resposta_link.status_code, 200)
        self.assertEqual(resposta_link.context['turno_bloqueado'], 'manha')

    def test_link_da_tarde_abre_exclusivamente_editar_tarde(self):
        semana = date(2026, 9, 14)
        self._editar(self.cliente_a, 'tarde', semana)

        response = self._diagnostico(self.cliente_a)
        conteudo = response.content.decode()

        link_tarde = self._link_editando('tarde', semana)
        self.assertIn(link_tarde, conteudo)
        self.assertIn('/laboratorios/cronograma/tarde/', link_tarde)
        self.assertNotIn('/laboratorios/cronograma/manha/', link_tarde)

        resposta_link = self.cliente_a.get(link_tarde)
        self.assertEqual(resposta_link.status_code, 200)
        self.assertEqual(resposta_link.context['turno_bloqueado'], 'tarde')

    def test_nenhum_link_de_em_edicao_usa_turno_todos_ou_rota_generica(self):
        semana_manha = date(2026, 9, 7)
        semana_tarde = date(2026, 9, 14)
        self._editar(self.cliente_a, 'manha', semana_manha)
        self._editar(self.cliente_a, 'tarde', semana_tarde)

        response = self._diagnostico(self.cliente_a)
        conteudo = response.content.decode()

        self.assertNotIn('turno=todos', conteudo)
        url_generica_isolada = f'href="{reverse("cronograma_semanal")}?data_inicio='
        self.assertNotIn(url_generica_isolada, conteudo)

    def test_cronograma_ativo_continua_exibido_corretamente(self):
        semana_ativa_manha = date(2026, 8, 24)
        semana_ativa_tarde = date(2026, 8, 17)
        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=semana_ativa_manha, horario='1', turno='manha',
            professor=self.professor, turma=self.turma_manha, disciplina='Aula manha',
            registrado_por=self.admin_a,
        )
        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=semana_ativa_tarde, horario='1', turno='tarde',
            professor=self.professor, turma=self.turma_tarde, disciplina='Aula tarde',
            registrado_por=self.admin_a,
        )
        self.cliente_a.post(
            reverse('ativar_cronograma_laboratorio'),
            {'turno': 'manha', 'data_inicio': semana_ativa_manha.isoformat()},
        )
        self.cliente_a.post(
            reverse('ativar_cronograma_laboratorio'),
            {'turno': 'tarde', 'data_inicio': semana_ativa_tarde.isoformat()},
        )

        response = self._diagnostico(self.cliente_b)
        self.assertContains(response, f'desde {semana_ativa_manha.strftime("%d/%m/%Y")}')
        self.assertContains(response, f'desde {semana_ativa_tarde.strftime("%d/%m/%Y")}')
        self.assertEqual(response.context['semana_ativa_manha_inicio'], semana_ativa_manha)
        self.assertEqual(response.context['semana_ativa_tarde_inicio'], semana_ativa_tarde)


class BotoesAtivarECancelarEdicaoMostramDataTests(TestCase):
    """Cobre a melhoria puramente visual: os botoes 'Ativar' e 'Cancelar
    edicao' do diagnostico devem exibir a data (dd/mm) da semana em edicao
    do respectivo turno, sem alterar a logica de qual data e submetida."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username='admin-botoes-cartao', password='senha-teste', is_superuser=True,
        )
        self.cliente = Client()
        self.cliente.force_login(self.admin)

        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.turma_manha = Turma.objects.create(nome='1A', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.turma_tarde = Turma.objects.create(nome='6A', ano=2026, serie='6 Ano', ativa=True, turno='tarde')
        self.professor = Professor.objects.create(nome_completo='Fabio Henrique Alencar', ativo=True)

    def _editar(self, turno, data_inicio):
        return self.cliente.post(
            reverse('editar_semana_cronograma_laboratorio'),
            {'turno': turno, 'data_inicio': data_inicio.isoformat()},
        )

    def _ativar_semana_com_agendamento(self, turno, data_inicio, turma):
        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=data_inicio, horario='1', turno=turno,
            professor=self.professor, turma=turma, disciplina='Aula ativa',
            registrado_por=self.admin,
        )
        return self.cliente.post(
            reverse('ativar_cronograma_laboratorio'),
            {'turno': turno, 'data_inicio': data_inicio.isoformat()},
        )

    def _diagnostico(self):
        response = self.cliente.get(reverse('diagnostico_cronograma_laboratorios'))
        return response.content.decode()

    def test_botao_ativar_manha_mostra_data_da_semana_editando(self):
        semana_ativa_manha = date(2026, 8, 24)
        semana_editando_manha = date(2026, 8, 17)
        self._ativar_semana_com_agendamento('manha', semana_ativa_manha, self.turma_manha)
        self._editar('manha', semana_editando_manha)

        conteudo = self._diagnostico()

        self.assertIn('▶️ Ativar manhã — 17/08', conteudo)
        self.assertIsNone(re.search(r'Ativar manhã\s*</button>', conteudo))

    def test_bloco_cancelar_edicao_manha_mostra_data(self):
        semana_editando_manha = date(2026, 8, 17)
        self._editar('manha', semana_editando_manha)

        conteudo = self._diagnostico()

        self.assertIn('Cancelar edição 17/08', conteudo)

    def test_botao_ativar_tarde_mostra_data_da_semana_editando(self):
        semana_ativa_tarde = date(2026, 8, 24)
        semana_editando_tarde = date(2026, 8, 10)
        self._ativar_semana_com_agendamento('tarde', semana_ativa_tarde, self.turma_tarde)
        self._editar('tarde', semana_editando_tarde)

        conteudo = self._diagnostico()

        self.assertIn('▶️ Ativar tarde — 10/08', conteudo)
        self.assertIsNone(re.search(r'Ativar tarde\s*</button>', conteudo))

    def test_bloco_cancelar_edicao_tarde_mostra_data(self):
        semana_editando_tarde = date(2026, 8, 10)
        self._editar('tarde', semana_editando_tarde)

        conteudo = self._diagnostico()

        self.assertIn('Cancelar edição 10/08', conteudo)

    def test_datas_de_manha_e_tarde_nao_se_misturam(self):
        semana_editando_manha = date(2026, 8, 17)
        semana_editando_tarde = date(2026, 8, 10)
        self._editar('manha', semana_editando_manha)
        self._editar('tarde', semana_editando_tarde)

        conteudo = self._diagnostico()
        contexto = self.cliente.get(reverse('diagnostico_cronograma_laboratorios')).context

        self.assertIn('▶️ Ativar manhã — 17/08', conteudo)
        self.assertIn('▶️ Ativar tarde — 10/08', conteudo)
        self.assertIn('Cancelar edição 17/08', conteudo)
        self.assertIn('Cancelar edição 10/08', conteudo)

        # isola o botao de ativar de cada turno pelo campo hidden "turno" que
        # o precede no mesmo <form>, para garantir que a data exibida no
        # texto do botao pertence de fato ao turno correto (sem troca cruzada)
        bloco_form_manha = re.search(
            r'name="turno" value="manha">.*?</form>', conteudo, re.DOTALL,
        )
        bloco_form_tarde = re.search(
            r'name="turno" value="tarde">.*?</form>', conteudo, re.DOTALL,
        )
        self.assertIsNotNone(bloco_form_manha)
        self.assertIsNotNone(bloco_form_tarde)
        self.assertIn('Ativar manhã — 17/08', bloco_form_manha.group(0))
        self.assertNotIn('10/08', bloco_form_manha.group(0))
        self.assertIn('Ativar tarde — 10/08', bloco_form_tarde.group(0))
        self.assertNotIn('17/08', bloco_form_tarde.group(0))

        self.assertEqual(contexto['semana_editando_manha_inicio'], semana_editando_manha)
        self.assertEqual(contexto['semana_editando_tarde_inicio'], semana_editando_tarde)

    def test_sem_semana_em_edicao_botoes_mantem_texto_original_e_desabilitados(self):
        conteudo = self._diagnostico()

        self.assertIn('▶️ Ativar manhã', conteudo)
        self.assertIn('▶️ Ativar tarde', conteudo)
        self.assertNotIn('Cancelar edição', conteudo)

        response = self.cliente.get(reverse('diagnostico_cronograma_laboratorios'))
        self.assertContains(response, 'disabled', count=2)

    def test_value_do_hidden_input_data_inicio_permanece_igual_ao_ativar(self):
        semana_editando_manha = date(2026, 8, 17)
        semana_editando_tarde = date(2026, 8, 10)
        self._editar('manha', semana_editando_manha)
        self._editar('tarde', semana_editando_tarde)

        conteudo = self._diagnostico()

        self.assertIn(
            '<input type="hidden" name="data_inicio" value="2026-08-17">', conteudo,
        )
        self.assertIn(
            '<input type="hidden" name="data_inicio" value="2026-08-10">', conteudo,
        )


class EditarCronogramaAtivoTests(TestCase):
    """Cobre a edicao direta do cronograma ja ativo, sem passar pelo fluxo de nova semana."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='diretivo-ativo', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.user)
        self.turma = Turma.objects.create(nome='2B', ano=2026, serie='2 Ano', ativa=True, turno='manha')
        self.professor_manha = Professor.objects.create(nome_completo='Carla Souza Melo', ativo=True)
        self.professor_tarde = Professor.objects.create(nome_completo='Diego Alves Rocha', ativo=True)
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.semana_manha = date(2026, 5, 25)
        self.semana_tarde = date(2026, 5, 18)

        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=self.semana_manha, horario='1', turno='manha',
            professor=self.professor_manha, turma=self.turma, disciplina='Matematica',
            registrado_por=self.user,
        )
        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=self.semana_tarde, horario='1', turno='tarde',
            professor=self.professor_tarde, turma=self.turma, disciplina='Historia',
            registrado_por=self.user,
        )

        self._preparar_e_ativar('manha', self.semana_manha)
        self._preparar_e_ativar('tarde', self.semana_tarde)

        # Garante que nenhum turno fica com uma semana "em edicao" pendente,
        # simulando o cenario real: cronograma so ativo, nada em preparacao.
        self.client.post(reverse('cancelar_edicao_semana_cronograma_laboratorio'), {'turno': 'manha'})
        self.client.post(reverse('cancelar_edicao_semana_cronograma_laboratorio'), {'turno': 'tarde'})

    def _preparar_e_ativar(self, turno, data_inicio):
        self.client.post(
            reverse('editar_semana_cronograma_laboratorio'),
            {'turno': turno, 'data_inicio': data_inicio.isoformat()},
        )
        self.client.post(
            reverse('ativar_cronograma_laboratorio'),
            {'turno': turno, 'data_inicio': data_inicio.isoformat()},
        )

    def _salvar_agendamento_no_ativo(self, turno, data, horario, professor):
        return self.client.post(reverse('cronograma_semanal_turno', kwargs={'turno_forcado': turno}), {
            'laboratorio_id': self.laboratorio.id,
            'data': data.isoformat(),
            'horario': horario,
            'turno': turno,
            'professor': professor.id,
            'turma': self.turma.id,
            'disciplina': 'Fisica',
            'observacao': '',
            'data_inicio': data.isoformat(),
            'turno_filtro': turno,
            'laboratorio_filtro': '',
            'scroll_y': '',
        })

    def test_cronograma_ativo_manha_pode_ser_aberto_para_edicao(self):
        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}),
            {'data_inicio': self.semana_manha.isoformat()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['data_inicio'], self.semana_manha)
        # Semana aberta e exatamente a semana ativa da manha - o cabecalho deve
        # dizer 'Cronograma Ativo', nao 'Editando' (ver TituloCronogramaAtivoPorTurnoTests).
        self.assertIn('Cronograma Ativo', response.context['cronograma_titulo'])
        self.assertIn('Manhã', response.context['cronograma_titulo'])

    def test_cronograma_ativo_tarde_pode_ser_aberto_para_edicao(self):
        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'}),
            {'data_inicio': self.semana_tarde.isoformat()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['data_inicio'], self.semana_tarde)
        self.assertIn('Cronograma Ativo', response.context['cronograma_titulo'])
        self.assertIn('Tarde', response.context['cronograma_titulo'])

    def test_editar_ativo_manha_nao_altera_ativo_tarde(self):
        tarde_antes = obter_cronograma_ativo('tarde')

        response = self._salvar_agendamento_no_ativo(
            'manha', self.semana_manha, '2', self.professor_manha,
        )

        self.assertRedirects(
            response,
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
            + f'?data_inicio={self.semana_manha.isoformat()}',
        )
        self.assertEqual(obter_cronograma_ativo('tarde'), tarde_antes)

    def test_editar_ativo_tarde_nao_altera_ativo_manha(self):
        manha_antes = obter_cronograma_ativo('manha')

        response = self._salvar_agendamento_no_ativo(
            'tarde', self.semana_tarde, '2', self.professor_tarde,
        )

        self.assertRedirects(
            response,
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'})
            + f'?data_inicio={self.semana_tarde.isoformat()}',
        )
        self.assertEqual(obter_cronograma_ativo('manha'), manha_antes)

    def test_editar_ativo_nao_altera_data_inicio(self):
        manha_antes_inicio, manha_antes_fim = obter_cronograma_ativo('manha')

        self._salvar_agendamento_no_ativo('manha', self.semana_manha, '3', self.professor_manha)

        manha_depois_inicio, manha_depois_fim = obter_cronograma_ativo('manha')
        self.assertEqual(manha_antes_inicio, manha_depois_inicio)
        self.assertEqual(manha_antes_fim, manha_depois_fim)
        self.assertEqual(manha_depois_inicio, self.semana_manha)

    def test_editar_ativo_nao_exige_nova_ativacao(self):
        configuracoes_antes = ConfiguracaoCronogramaLaboratorio.objects.count()

        self._salvar_agendamento_no_ativo('manha', self.semana_manha, '4', self.professor_manha)

        ativo_inicio, _ = obter_cronograma_ativo('manha')
        self.assertEqual(ativo_inicio, self.semana_manha)
        self.assertEqual(ConfiguracaoCronogramaLaboratorio.objects.count(), configuracoes_antes)

        contexto = self.client.get(reverse('diagnostico_cronograma_laboratorios')).context
        self.assertIsNone(contexto['semana_editando_manha_inicio'])
        self.assertEqual(contexto['semana_ativa_manha_inicio'], self.semana_manha)

    def test_banner_laboratorio_identifica_turno_e_data_inicio(self):
        response_manha = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}),
            {'data_inicio': self.semana_manha.isoformat()},
        )
        response_tarde = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'}),
            {'data_inicio': self.semana_tarde.isoformat()},
        )

        self.assertEqual(
            response_manha.context['cronograma_titulo'],
            f'🟢 Cronograma Ativo — Manhã · Início: {self.semana_manha.strftime("%d/%m/%Y")}',
        )
        self.assertEqual(
            response_tarde.context['cronograma_titulo'],
            f'🟢 Cronograma Ativo — Tarde · Início: {self.semana_tarde.strftime("%d/%m/%Y")}',
        )

    def test_diagnostico_exibe_link_para_editar_cronograma_ativo(self):
        response = self.client.get(reverse('diagnostico_cronograma_laboratorios'))
        conteudo = response.content.decode()

        self.assertIn('CRONOGRAMA ATIVO', conteudo)
        # O link leva para a tela travada por turno (turno vem da URL, nao de
        # querystring) - ver EdicaoCronogramaTravadaPorTurnoTests.
        url_manha = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        url_tarde = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'})
        self.assertIn(
            f"{url_manha}?data_inicio={self.semana_manha.isoformat()}", conteudo,
        )
        self.assertIn(
            f"{url_tarde}?data_inicio={self.semana_tarde.isoformat()}", conteudo,
        )

    def test_fluxo_de_novo_cronograma_continua_funcionando(self):
        nova_semana_manha = self.semana_manha + timedelta(days=21)
        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=nova_semana_manha, horario='1', turno='manha',
            professor=self.professor_manha, turma=self.turma, disciplina='Geografia',
            registrado_por=self.user,
        )

        self.client.post(
            reverse('editar_semana_cronograma_laboratorio'),
            {'turno': 'manha', 'data_inicio': nova_semana_manha.isoformat()},
        )
        contexto = self.client.get(reverse('diagnostico_cronograma_laboratorios')).context
        self.assertEqual(contexto['semana_editando_manha_inicio'], nova_semana_manha)
        self.assertEqual(contexto['semana_ativa_manha_inicio'], self.semana_manha)

        self.client.post(
            reverse('ativar_cronograma_laboratorio'),
            {'turno': 'manha', 'data_inicio': nova_semana_manha.isoformat()},
        )
        ativo_inicio, _ = obter_cronograma_ativo('manha')
        self.assertEqual(ativo_inicio, nova_semana_manha)


class EdicaoCronogramaTravadaPorTurnoTests(TestCase):
    """Cobre a trava de turno nas telas 'Editar Manha'/'Editar Tarde'
    (/laboratorios/cronograma/manha/ e /laboratorios/cronograma/tarde/): o
    turno vem exclusivamente do caminho da URL, a opcao "Todos" nao existe
    nessas telas, e um POST feito numa delas nunca cria, edita ou exclui
    agendamentos do outro turno - mesmo que o parametro seja manipulado
    manualmente na querystring ou no corpo do POST."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='diretivo-turno-travado', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.user)
        self.turma_manha = Turma.objects.create(nome='1A', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.turma_tarde = Turma.objects.create(nome='6A', ano=2026, serie='6 Ano', ativa=True, turno='tarde')
        self.professor = Professor.objects.create(nome_completo='Ana Paula Rocha', ativo=True)
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.semana = date(2026, 8, 24)  # segunda-feira

        self.agendamento_manha = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=self.semana, horario='1', turno='manha',
            professor=self.professor, turma=self.turma_manha, disciplina='Matematica',
            registrado_por=self.user,
        )
        self.agendamento_tarde = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=self.semana, horario='1', turno='tarde',
            professor=self.professor, turma=self.turma_tarde, disciplina='Historia',
            registrado_por=self.user,
        )

    def _post_base(self, **overrides):
        dados = {
            'laboratorio_id': self.laboratorio.id,
            'data': self.semana.isoformat(),
            'horario': '3',
            'professor': self.professor.id,
            'disciplina': 'Aula generica',
            'observacao': '',
            'data_inicio': self.semana.isoformat(),
            'laboratorio_filtro': '',
            'scroll_y': '',
        }
        dados.update(overrides)
        return dados

    # --- turno=todos / parametros manipulados nao sao aceitos -------------

    def test_url_com_turno_todos_nao_existe(self):
        response = self.client.get('/laboratorios/cronograma/todos/')
        self.assertEqual(response.status_code, 404)

    def test_url_com_turno_invalido_nao_existe(self):
        response = self.client.get('/laboratorios/cronograma/qualquercoisa/')
        self.assertEqual(response.status_code, 404)

    def test_querystring_turno_e_ignorada_na_tela_de_manha(self):
        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}),
            {'turno': 'tarde', 'data_inicio': self.semana.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['turno_filtro'], 'manha')
        self.assertEqual(response.context['turno_bloqueado'], 'manha')
        self.assertContains(response, 'Matematica')
        self.assertNotContains(response, 'Historia')

    def test_querystring_turno_e_ignorada_na_tela_de_tarde(self):
        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'}),
            {'turno': 'manha', 'data_inicio': self.semana.isoformat()},
        )
        self.assertEqual(response.context['turno_filtro'], 'tarde')
        self.assertContains(response, 'Historia')
        self.assertNotContains(response, 'Matematica')

    # --- a opcao "Todos" desaparece da interface de edicao -----------------

    def test_opcao_todos_nao_aparece_na_tela_de_manha(self):
        response = self.client.get(reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}))
        self.assertNotContains(response, '<select name="turno"')
        self.assertContains(response, 'data-turno-travado="manha"')

    def test_opcao_todos_nao_aparece_na_tela_de_tarde(self):
        response = self.client.get(reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'}))
        self.assertNotContains(response, '<select name="turno"')
        self.assertContains(response, 'data-turno-travado="tarde"')

    def test_rota_geral_sem_turno_redireciona_para_diagnostico(self):
        """A entrada generica nao mantem uma terceira selecao de semana."""
        response = self.client.get(reverse('cronograma_semanal'))
        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))

    # --- manha nunca altera tarde, e vice-versa -----------------------------

    def test_post_na_tela_de_manha_forja_turno_tarde_mas_grava_como_manha(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        total_tarde_antes = AgendamentoLab.objects.filter(turno='tarde').count()

        self.client.post(url, self._post_base(
            horario='4', turno='tarde', turma=self.turma_tarde.id, disciplina='Forjado',
        ))

        novo = AgendamentoLab.objects.filter(disciplina='Forjado').first()
        self.assertIsNotNone(novo)
        self.assertEqual(novo.turno, 'manha')
        self.assertEqual(AgendamentoLab.objects.filter(turno='tarde').count(), total_tarde_antes)

    def test_post_na_tela_de_tarde_forja_turno_manha_mas_grava_como_tarde(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'})
        total_manha_antes = AgendamentoLab.objects.filter(turno='manha').count()

        self.client.post(url, self._post_base(
            horario='4', turno='manha', turma=self.turma_manha.id, disciplina='Forjado',
        ))

        novo = AgendamentoLab.objects.filter(disciplina='Forjado').first()
        self.assertIsNotNone(novo)
        self.assertEqual(novo.turno, 'tarde')
        self.assertEqual(AgendamentoLab.objects.filter(turno='manha').count(), total_manha_antes)

    def test_post_na_tela_de_manha_nao_consegue_editar_agendamento_de_tarde(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})

        response = self.client.post(url, self._post_base(
            agendamento_id=self.agendamento_tarde.id,
            horario='1', turno='manha', turma=self.turma_tarde.id, disciplina='Alterado indevidamente',
        ))

        self.agendamento_tarde.refresh_from_db()
        self.assertEqual(self.agendamento_tarde.disciplina, 'Historia')
        self.assertEqual(self.agendamento_tarde.turno, 'tarde')
        self.assertRedirects(response, reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}) + f'?data_inicio={self.semana.isoformat()}')

    def test_post_na_tela_de_tarde_nao_consegue_editar_agendamento_de_manha(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'})

        response = self.client.post(url, self._post_base(
            agendamento_id=self.agendamento_manha.id,
            horario='1', turno='tarde', turma=self.turma_manha.id, disciplina='Alterado indevidamente',
        ))

        self.agendamento_manha.refresh_from_db()
        self.assertEqual(self.agendamento_manha.disciplina, 'Matematica')
        self.assertEqual(self.agendamento_manha.turno, 'manha')
        self.assertRedirects(response, reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'}) + f'?data_inicio={self.semana.isoformat()}')

    def test_post_na_tela_de_manha_nao_consegue_excluir_agendamento_de_tarde_via_edicao(self):
        """A edicao travada em manha nao oferece nenhum caminho de escrita que
        alcance um AgendamentoLab de tarde: tentar 'reaproveitar' o id de um
        agendamento de tarde e rejeitado sem tocar no registro (ver teste
        acima). Aqui confirmamos que o registro de tarde nem desaparece nem
        e duplicado depois dessa tentativa."""
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        total_antes = AgendamentoLab.objects.count()

        self.client.post(url, self._post_base(
            agendamento_id=self.agendamento_tarde.id,
            horario='1', turno='manha', turma=self.turma_tarde.id, disciplina='Nao deveria salvar',
        ))

        self.assertEqual(AgendamentoLab.objects.count(), total_antes)
        self.assertTrue(AgendamentoLab.objects.filter(id=self.agendamento_tarde.id, turno='tarde').exists())

    # --- fluxos normais continuam funcionando -------------------------------

    def test_fluxo_normal_de_criacao_na_tela_de_manha(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        response = self.client.post(url, self._post_base(
            horario='2', turno='manha', turma=self.turma_manha.id, disciplina='Fisica',
        ))
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Fisica', turno='manha').exists())
        self.assertRedirects(response, url + f'?data_inicio={self.semana.isoformat()}')

    def test_fluxo_normal_de_criacao_na_tela_de_tarde(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'})
        response = self.client.post(url, self._post_base(
            horario='2', turno='tarde', turma=self.turma_tarde.id, disciplina='Geografia',
        ))
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Geografia', turno='tarde').exists())
        self.assertRedirects(response, url + f'?data_inicio={self.semana.isoformat()}')

    def test_fluxo_normal_de_edicao_do_proprio_turno_na_tela_de_manha(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        response = self.client.post(url, self._post_base(
            agendamento_id=self.agendamento_manha.id,
            horario='1', turno='manha', turma=self.turma_manha.id, disciplina='Matematica Avancada',
        ))
        self.agendamento_manha.refresh_from_db()
        self.assertEqual(self.agendamento_manha.disciplina, 'Matematica Avancada')
        self.assertRedirects(response, url + f'?data_inicio={self.semana.isoformat()}')

    def test_filtro_de_laboratorio_continua_funcionando_na_tela_travada(self):
        outro_lab = Laboratorio.objects.create(nome='Laboratorio 2', tipo='fixo', equipamento='Notebook', ativo=True)
        AgendamentoLab.objects.create(
            laboratorio=outro_lab, data=self.semana, horario='1', turno='manha',
            professor=self.professor, turma=self.turma_manha, disciplina='Quimica',
            registrado_por=self.user,
        )
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})

        response = self.client.get(url, {'laboratorio': self.laboratorio.id, 'data_inicio': self.semana.isoformat()})
        self.assertContains(response, 'Matematica')
        self.assertNotContains(response, 'Quimica')

    def test_pagina_de_edicao_oferece_professores_e_turmas_normalmente(self):
        response = self.client.get(reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}))
        self.assertIn(self.professor, list(response.context['professores']))
        self.assertIn(self.turma_manha, list(response.context['turmas']))
        self.assertIn(self.turma_tarde, list(response.context['turmas']))


class TituloCronogramaAtivoPorTurnoTests(TestCase):
    """Cobre o cabecalho (cronograma_titulo) da tela de edicao travada por
    turno: deve dizer 'Cronograma Ativo' quando a semana aberta e exatamente
    a semana ativa DAQUELE turno (obter_cronograma_ativo(turno)). Manha e
    tarde tem semanas ativas diferentes de proposito neste setUp, para
    provar que cada turno so compara com o seu proprio periodo ativo. Os
    outros dois estados ('Editando Cronograma' quando bate com o marcador
    real, 'Visualizando Semana' quando nao bate com nada) sao cobertos com
    mais detalhe em TituloCronogramaTresEstadosTests."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='diretivo-titulo', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.user)
        self.turma = Turma.objects.create(nome='1A', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.professor = Professor.objects.create(nome_completo='Bianca Freitas', ativo=True)
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True,
        )

        # Semanas ativas diferentes por turno, de proposito.
        self.semana_ativa_manha = date(2026, 8, 24)
        self.semana_ativa_tarde = date(2026, 8, 17)

        for semana in (self.semana_ativa_manha, self.semana_ativa_tarde):
            AgendamentoLab.objects.create(
                laboratorio=self.laboratorio, data=semana, horario='1', turno='manha',
                professor=self.professor, turma=self.turma, disciplina='Aula',
                registrado_por=self.user,
            )
            AgendamentoLab.objects.create(
                laboratorio=self.laboratorio, data=semana, horario='2', turno='tarde',
                professor=self.professor, turma=self.turma, disciplina='Aula',
                registrado_por=self.user,
            )

        ConfiguracaoCronogramaLaboratorio.objects.create(
            data_inicio_cronograma_ativo=self.semana_ativa_manha,
            data_fim_cronograma_ativo=self.semana_ativa_manha + timedelta(days=6),
            data_inicio_manha_ativo=self.semana_ativa_manha,
            data_fim_manha_ativo=self.semana_ativa_manha + timedelta(days=6),
            data_inicio_tarde_ativo=self.semana_ativa_tarde,
            data_fim_tarde_ativo=self.semana_ativa_tarde + timedelta(days=6),
            atualizado_por=self.user,
        )

    def test_semana_ativa_da_manha_mostra_cronograma_ativo(self):
        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}),
            {'data_inicio': self.semana_ativa_manha.isoformat()},
        )
        self.assertEqual(
            response.context['cronograma_titulo'],
            f'🟢 Cronograma Ativo — Manhã · Início: {self.semana_ativa_manha.strftime("%d/%m/%Y")}',
        )

    def test_semana_ativa_da_tarde_mostra_cronograma_ativo(self):
        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'}),
            {'data_inicio': self.semana_ativa_tarde.isoformat()},
        )
        self.assertEqual(
            response.context['cronograma_titulo'],
            f'🟢 Cronograma Ativo — Tarde · Início: {self.semana_ativa_tarde.strftime("%d/%m/%Y")}',
        )

    def test_semana_nao_ativa_da_manha_mostra_editando_cronograma(self):
        outra_semana = self.semana_ativa_manha + timedelta(days=7)
        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=outra_semana, horario='3', turno='manha',
            professor=self.professor, turma=self.turma, disciplina='Aula futura',
            registrado_por=self.user,
        )
        # So mostra "Editando" se a data bater com o marcador REAL do banco.
        ConfiguracaoCronogramaLaboratorio.objects.update(data_inicio_manha_editando=outra_semana)

        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}),
            {'data_inicio': outra_semana.isoformat()},
        )
        self.assertEqual(
            response.context['cronograma_titulo'],
            f'📝 Editando Cronograma — Manhã · Início: {outra_semana.strftime("%d/%m/%Y")}',
        )

    def test_semana_nao_ativa_da_tarde_mostra_editando_cronograma(self):
        outra_semana = self.semana_ativa_tarde + timedelta(days=7)
        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=outra_semana, horario='3', turno='tarde',
            professor=self.professor, turma=self.turma, disciplina='Aula futura',
            registrado_por=self.user,
        )
        ConfiguracaoCronogramaLaboratorio.objects.update(data_inicio_tarde_editando=outra_semana)

        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'}),
            {'data_inicio': outra_semana.isoformat()},
        )
        self.assertEqual(
            response.context['cronograma_titulo'],
            f'📝 Editando Cronograma — Tarde · Início: {outra_semana.strftime("%d/%m/%Y")}',
        )

    def test_manha_nunca_usa_estado_ativo_da_tarde(self):
        """A semana ativa da TARDE nao e a semana ativa da MANHA (setUp usa
        semanas diferentes de proposito) - abrir essa semana na tela travada
        em manha nunca pode mostrar 'Ativo'. Como ninguem marcou essa data
        como semana em edicao da manha, o titulo correto e o terceiro
        estado ('Visualizando Semana'), nao 'Editando'."""
        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}),
            {'data_inicio': self.semana_ativa_tarde.isoformat()},
        )
        self.assertIn('Visualizando Semana — Manhã', response.context['cronograma_titulo'])
        self.assertNotIn('Cronograma Ativo', response.context['cronograma_titulo'])
        self.assertNotIn('Editando Cronograma', response.context['cronograma_titulo'])

    def test_tarde_nunca_usa_estado_ativo_da_manha(self):
        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'}),
            {'data_inicio': self.semana_ativa_manha.isoformat()},
        )
        self.assertIn('Visualizando Semana — Tarde', response.context['cronograma_titulo'])
        self.assertNotIn('Cronograma Ativo', response.context['cronograma_titulo'])
        self.assertNotIn('Editando Cronograma', response.context['cronograma_titulo'])

    def test_data_final_nao_aparece_no_cabecalho(self):
        response_manha = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}),
            {'data_inicio': self.semana_ativa_manha.isoformat()},
        )
        fim_manha = (self.semana_ativa_manha + timedelta(days=6)).strftime('%d/%m/%Y')
        self.assertNotIn(fim_manha, response_manha.context['cronograma_titulo'])

        response_tarde = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'}),
            {'data_inicio': self.semana_ativa_tarde.isoformat()},
        )
        fim_tarde = (self.semana_ativa_tarde + timedelta(days=6)).strftime('%d/%m/%Y')
        self.assertNotIn(fim_tarde, response_tarde.context['cronograma_titulo'])


class TituloCronogramaTresEstadosTests(TestCase):
    """Cobre a correcao do bug relatado: abrir /laboratorios/cronograma/<turno>/
    com uma data avulsa nunca pode dizer 'Editando Cronograma' se essa data
    nao bate com o marcador real (data_inicio_manha_editando /
    data_inicio_tarde_editando). O titulo agora tem tres estados, nesta
    ordem de prioridade: Ativo > Editando (so se bater com o marcador
    compartilhado) > Visualizando Semana (qualquer outra data). Manha e
    tarde tem periodos ativos e marcadores de edicao TODOS diferentes entre
    si de proposito, para provar isolamento total entre os dois turnos."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='diretivo-tres-estados', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.user)
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True,
        )

        self.semana_ativa_manha = date(2026, 8, 24)
        self.semana_ativa_tarde = date(2026, 8, 17)
        self.semana_editando_manha = date(2026, 8, 31)
        self.semana_editando_tarde = date(2026, 9, 7)
        self.semana_avulsa = date(2026, 9, 14)  # nao e ativa nem editando em nenhum turno

        self.configuracao = ConfiguracaoCronogramaLaboratorio.objects.create(
            data_inicio_cronograma_ativo=self.semana_ativa_manha,
            data_fim_cronograma_ativo=self.semana_ativa_manha + timedelta(days=6),
            data_inicio_manha_ativo=self.semana_ativa_manha,
            data_fim_manha_ativo=self.semana_ativa_manha + timedelta(days=6),
            data_inicio_tarde_ativo=self.semana_ativa_tarde,
            data_fim_tarde_ativo=self.semana_ativa_tarde + timedelta(days=6),
            data_inicio_manha_editando=self.semana_editando_manha,
            data_inicio_tarde_editando=self.semana_editando_tarde,
            atualizado_por=self.user,
        )

    def _titulo(self, turno, data_inicio):
        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': turno}),
            {'data_inicio': data_inicio.isoformat()},
        )
        return response, response.context['cronograma_titulo']

    # --- 1) semana ativa -----------------------------------------------

    def test_manha_semana_ativa_mostra_cronograma_ativo(self):
        _, titulo = self._titulo('manha', self.semana_ativa_manha)
        self.assertEqual(
            titulo, f'🟢 Cronograma Ativo — Manhã · Início: {self.semana_ativa_manha.strftime("%d/%m/%Y")}',
        )

    def test_tarde_semana_ativa_mostra_cronograma_ativo(self):
        _, titulo = self._titulo('tarde', self.semana_ativa_tarde)
        self.assertEqual(
            titulo, f'🟢 Cronograma Ativo — Tarde · Início: {self.semana_ativa_tarde.strftime("%d/%m/%Y")}',
        )

    # --- 2) semana marcada em _editando ---------------------------------

    def test_manha_semana_no_marcador_editando_mostra_editando_cronograma(self):
        _, titulo = self._titulo('manha', self.semana_editando_manha)
        self.assertEqual(
            titulo, f'📝 Editando Cronograma — Manhã · Início: {self.semana_editando_manha.strftime("%d/%m/%Y")}',
        )

    def test_tarde_semana_no_marcador_editando_mostra_editando_cronograma(self):
        _, titulo = self._titulo('tarde', self.semana_editando_tarde)
        self.assertEqual(
            titulo, f'📝 Editando Cronograma — Tarde · Início: {self.semana_editando_tarde.strftime("%d/%m/%Y")}',
        )

    # --- 3) semana com marcador None (o bug relatado) -------------------

    def test_manha_semana_avulsa_com_marcador_none_mostra_visualizando(self):
        """Reproduz o bug relatado: sem NENHUM marcador setado, uma URL
        avulsa nao pode se chamar 'Editando'."""
        ConfiguracaoCronogramaLaboratorio.objects.update(data_inicio_manha_editando=None)
        _, titulo = self._titulo('manha', self.semana_avulsa)
        self.assertEqual(
            titulo, f'🔍 Visualizando Semana — Manhã · Início: {self.semana_avulsa.strftime("%d/%m/%Y")}',
        )

    def test_tarde_semana_avulsa_com_marcador_none_mostra_visualizando(self):
        ConfiguracaoCronogramaLaboratorio.objects.update(data_inicio_tarde_editando=None)
        _, titulo = self._titulo('tarde', self.semana_avulsa)
        self.assertEqual(
            titulo, f'🔍 Visualizando Semana — Tarde · Início: {self.semana_avulsa.strftime("%d/%m/%Y")}',
        )

    # --- 4) terceira semana, diferente da ativa e da editando -----------

    def test_manha_terceira_semana_diferente_de_ativa_e_editando_mostra_visualizando(self):
        # marcador de edicao continua setado (self.semana_editando_manha),
        # mas a data pedida agora e uma TERCEIRA semana diferente dela.
        _, titulo = self._titulo('manha', self.semana_avulsa)
        self.assertEqual(
            titulo, f'🔍 Visualizando Semana — Manhã · Início: {self.semana_avulsa.strftime("%d/%m/%Y")}',
        )

    def test_tarde_terceira_semana_diferente_de_ativa_e_editando_mostra_visualizando(self):
        _, titulo = self._titulo('tarde', self.semana_avulsa)
        self.assertEqual(
            titulo, f'🔍 Visualizando Semana — Tarde · Início: {self.semana_avulsa.strftime("%d/%m/%Y")}',
        )

    # --- 5) e 6) abrir URL avulsa nao escreve nada -----------------------

    def test_abrir_url_avulsa_nao_grava_marcador_de_edicao(self):
        antes_manha = self.configuracao.data_inicio_manha_editando
        antes_tarde = self.configuracao.data_inicio_tarde_editando

        self._titulo('manha', self.semana_avulsa)
        self._titulo('tarde', self.semana_avulsa)

        self.configuracao.refresh_from_db()
        self.assertEqual(self.configuracao.data_inicio_manha_editando, antes_manha)
        self.assertEqual(self.configuracao.data_inicio_tarde_editando, antes_tarde)

    def test_abrir_url_avulsa_nao_altera_cronograma_ativo(self):
        antes_manha = (self.configuracao.data_inicio_manha_ativo, self.configuracao.data_fim_manha_ativo)
        antes_tarde = (self.configuracao.data_inicio_tarde_ativo, self.configuracao.data_fim_tarde_ativo)

        self._titulo('manha', self.semana_avulsa)
        self._titulo('tarde', self.semana_avulsa)

        self.configuracao.refresh_from_db()
        self.assertEqual(
            (self.configuracao.data_inicio_manha_ativo, self.configuracao.data_fim_manha_ativo), antes_manha,
        )
        self.assertEqual(
            (self.configuracao.data_inicio_tarde_ativo, self.configuracao.data_fim_tarde_ativo), antes_tarde,
        )

    # --- 7) e 8) isolamento entre turnos no titulo -----------------------

    def test_manha_nunca_usa_marcador_editando_da_tarde(self):
        # self.semana_editando_tarde e o marcador real da TARDE - abrir essa
        # mesma data na rota da MANHA nao pode mostrar "Editando".
        _, titulo = self._titulo('manha', self.semana_editando_tarde)
        self.assertNotIn('Editando Cronograma', titulo)
        self.assertIn('Visualizando Semana — Manhã', titulo)

    def test_tarde_nunca_usa_marcador_editando_da_manha(self):
        _, titulo = self._titulo('tarde', self.semana_editando_manha)
        self.assertNotIn('Editando Cronograma', titulo)
        self.assertIn('Visualizando Semana — Tarde', titulo)

    # --- 9) prioridade: ativo vence quando ativo e editando coincidem ----

    def test_quando_ativa_e_editando_coincidem_titulo_continua_ativo(self):
        mesma_semana = date(2026, 10, 5)
        ConfiguracaoCronogramaLaboratorio.objects.update(
            data_inicio_manha_ativo=mesma_semana,
            data_fim_manha_ativo=mesma_semana + timedelta(days=6),
            data_inicio_manha_editando=mesma_semana,
        )
        _, titulo = self._titulo('manha', mesma_semana)
        self.assertEqual(
            titulo, f'🟢 Cronograma Ativo — Manhã · Início: {mesma_semana.strftime("%d/%m/%Y")}',
        )
        self.assertNotIn('Editando', titulo)

    def test_quando_ativa_e_editando_coincidem_na_tarde_titulo_continua_ativo(self):
        mesma_semana = date(2026, 10, 12)
        ConfiguracaoCronogramaLaboratorio.objects.update(
            data_inicio_tarde_ativo=mesma_semana,
            data_fim_tarde_ativo=mesma_semana + timedelta(days=6),
            data_inicio_tarde_editando=mesma_semana,
        )
        _, titulo = self._titulo('tarde', mesma_semana)
        self.assertEqual(
            titulo, f'🟢 Cronograma Ativo — Tarde · Início: {mesma_semana.strftime("%d/%m/%Y")}',
        )
        self.assertNotIn('Editando', titulo)


class PrepararSemanaFormularioRealTests(TestCase):
    """Reproduz o POST real do formulario 'Preparar esta semana' do
    diagnostico (apps/templates/laboratorios/diagnostico_cronograma.html,
    forms #formPrepararManha / #formPrepararTarde) - via Client.post na URL
    resolvida de editar_semana_cronograma_laboratorio, com exatamente os
    campos que o form envia ('turno' hidden + 'data_inicio' do input
    type=date), nunca chamando a view isolada diretamente."""

    def setUp(self):
        self.admin_a = User.objects.create_user(
            username='admin-a-preparar', password='senha-teste', is_superuser=True,
        )
        self.admin_b = User.objects.create_user(
            username='admin-b-preparar', password='senha-teste', is_superuser=True,
        )
        self.cliente_a = Client()
        self.cliente_b = Client()
        self.cliente_a.force_login(self.admin_a)
        self.cliente_b.force_login(self.admin_b)

    def _post_form_real(self, cliente, turno, data_inicio):
        return cliente.post(
            reverse('editar_semana_cronograma_laboratorio'),
            {'turno': turno, 'data_inicio': data_inicio},
        )

    def test_post_formulario_manha_grava_data_inicio_manha_editando(self):
        # Fase 2: "Preparar/Criar semana" grava em SemanaCronogramaLaboratorio
        # (status='editando'), nao mais no campo antigo data_inicio_manha_editando -
        # esse campo antigo nao consegue representar varias semanas em edicao e
        # deixou de ser escrito por este fluxo (ver relatorio da Fase 2, secao 8).
        self._post_form_real(self.cliente_a, 'manha', '2026-08-31')
        self.assertTrue(
            SemanaCronogramaLaboratorio.objects.filter(
                turno='manha', data_inicio=date(2026, 8, 31), status='editando',
            ).exists()
        )

    def test_post_formulario_manha_nao_altera_data_inicio_tarde_editando(self):
        SemanaCronogramaLaboratorio.objects.create(
            turno='tarde', data_inicio=date(2026, 9, 21), status='editando',
        )
        self._post_form_real(self.cliente_a, 'manha', '2026-08-31')
        self.assertTrue(
            SemanaCronogramaLaboratorio.objects.filter(
                turno='manha', data_inicio=date(2026, 8, 31), status='editando',
            ).exists()
        )
        self.assertTrue(
            SemanaCronogramaLaboratorio.objects.filter(
                turno='tarde', data_inicio=date(2026, 9, 21), status='editando',
            ).exists()
        )

    def test_post_formulario_tarde_grava_data_inicio_tarde_editando(self):
        self._post_form_real(self.cliente_a, 'tarde', '2026-09-07')
        self.assertTrue(
            SemanaCronogramaLaboratorio.objects.filter(
                turno='tarde', data_inicio=date(2026, 9, 7), status='editando',
            ).exists()
        )

    def test_post_formulario_tarde_nao_altera_data_inicio_manha_editando(self):
        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 8, 31), status='editando',
        )
        self._post_form_real(self.cliente_a, 'tarde', '2026-09-07')
        self.assertTrue(
            SemanaCronogramaLaboratorio.objects.filter(
                turno='tarde', data_inicio=date(2026, 9, 7), status='editando',
            ).exists()
        )
        self.assertTrue(
            SemanaCronogramaLaboratorio.objects.filter(
                turno='manha', data_inicio=date(2026, 8, 31), status='editando',
            ).exists()
        )

    def test_data_invalida_e_recusada_e_nao_grava_nada(self):
        self._post_form_real(self.cliente_a, 'manha', 'nao-e-uma-data-valida')
        self.assertFalse(ConfiguracaoCronogramaLaboratorio.objects.exists())

    def test_turno_todos_e_recusado_e_nao_grava_nada(self):
        self._post_form_real(self.cliente_a, 'todos', '2026-08-31')
        self.assertFalse(ConfiguracaoCronogramaLaboratorio.objects.exists())

    def test_redirecionamento_apos_preparar_manha_leva_a_rota_travada_de_manha(self):
        response = self._post_form_real(self.cliente_a, 'manha', '2026-08-31')
        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))

        resposta_diagnostico = self.cliente_a.get(reverse('diagnostico_cronograma_laboratorios'))
        url_manha = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        self.assertContains(resposta_diagnostico, f'{url_manha}?data_inicio=2026-08-31')

    def test_redirecionamento_apos_preparar_tarde_leva_a_rota_travada_de_tarde(self):
        response = self._post_form_real(self.cliente_a, 'tarde', '2026-09-07')
        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))

        resposta_diagnostico = self.cliente_a.get(reverse('diagnostico_cronograma_laboratorios'))
        url_tarde = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'})
        self.assertContains(resposta_diagnostico, f'{url_tarde}?data_inicio=2026-09-07')

    def test_admin_b_enxerga_semana_preparada_pelo_admin_a_via_post_real(self):
        self._post_form_real(self.cliente_a, 'manha', '2026-08-31')

        resposta_b = self.cliente_b.get(reverse('diagnostico_cronograma_laboratorios'))
        self.assertEqual(resposta_b.context['semana_editando_manha_inicio'], date(2026, 8, 31))

    def test_cartao_mostra_em_edicao_depois_do_post_real(self):
        self._post_form_real(self.cliente_a, 'manha', '2026-08-31')

        resposta = self.cliente_a.get(reverse('diagnostico_cronograma_laboratorios'))
        self.assertContains(resposta, '📝 Em edição')

    def test_primeira_segunda_e_terceira_semana_vazias_aparecem_imediatamente(self):
        datas = ('2026-08-31', '2026-09-07', '2026-09-14')

        for quantidade, data_inicio in enumerate(datas, start=1):
            response = self._post_form_real(self.cliente_a, 'manha', data_inicio)
            self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))

            diagnostico = self.cliente_a.get(reverse('diagnostico_cronograma_laboratorios'))
            semanas = diagnostico.context['semanas_editando_manha']
            self.assertEqual(len(semanas), quantidade)
            self.assertEqual(
                [semana.data_inicio.isoformat() for semana in semanas], list(datas[:quantidade]),
            )
            self.assertEqual(AgendamentoLab.objects.count(), 0)

    def test_diagnostico_ignora_marcadores_legados_de_edicao(self):
        ConfiguracaoCronogramaLaboratorio.objects.create(
            data_inicio_cronograma_ativo=date(2029, 12, 3),
            data_fim_cronograma_ativo=date(2029, 12, 9),
            data_inicio_manha_editando=date(2030, 1, 7),
            data_inicio_tarde_editando=date(2030, 1, 14),
        )

        diagnostico = self.cliente_a.get(reverse('diagnostico_cronograma_laboratorios'))

        self.assertEqual(diagnostico.context['semanas_editando_manha'], [])
        self.assertEqual(diagnostico.context['semanas_editando_tarde'], [])
        self.assertIsNone(diagnostico.context['semana_editando_manha_inicio'])
        self.assertIsNone(diagnostico.context['semana_editando_tarde_inicio'])

    def test_diagnostico_filtra_status_e_mantem_ordem_crescente(self):
        for status, data_inicio in (
            ('historica', date(2026, 8, 3)),
            ('ativa', date(2026, 8, 10)),
            ('cancelada', date(2026, 8, 17)),
            ('editando', date(2026, 9, 14)),
            ('editando', date(2026, 8, 31)),
            ('editando', date(2026, 9, 7)),
        ):
            SemanaCronogramaLaboratorio.objects.create(
                turno='manha', data_inicio=data_inicio, status=status,
            )

        diagnostico = self.cliente_a.get(reverse('diagnostico_cronograma_laboratorios'))

        self.assertEqual(
            [semana.data_inicio for semana in diagnostico.context['semanas_editando_manha']],
            [date(2026, 8, 31), date(2026, 9, 7), date(2026, 9, 14)],
        )

    def test_listas_do_diagnostico_sao_independentes_por_turno(self):
        self._post_form_real(self.cliente_a, 'manha', '2026-08-31')
        self._post_form_real(self.cliente_a, 'tarde', '2026-09-07')

        diagnostico = self.cliente_b.get(reverse('diagnostico_cronograma_laboratorios'))

        self.assertEqual(
            [semana.data_inicio for semana in diagnostico.context['semanas_editando_manha']],
            [date(2026, 8, 31)],
        )
        self.assertEqual(
            [semana.data_inicio for semana in diagnostico.context['semanas_editando_tarde']],
            [date(2026, 9, 7)],
        )

    def test_preparar_semana_via_formulario_nao_cria_nenhum_agendamentolab(self):
        total_antes = AgendamentoLab.objects.count()

        self._post_form_real(self.cliente_a, 'manha', '2026-08-31')
        self._post_form_real(self.cliente_b, 'tarde', '2026-09-07')

        self.assertEqual(AgendamentoLab.objects.count(), total_antes)


class EscritaGradeRestritaAAtivaOuEditandoTests(TestCase):
    """Cobre a correcao do bug de producao: POST na grade travada por turno
    (cronograma_semanal_turno) so pode criar/editar AgendamentoLab quando a
    semana solicitada e a ATIVA daquele turno ou a marcada como EM EDICAO
    (marcador compartilhado real). Qualquer outra semana - mesmo com uma URL
    valida e um POST bem formado - deve ser recusada sem gravar nada. Foi
    exatamente a falta dessa checagem que permitiu criar os 25 AgendamentoLab
    da semana de 17/08/2026 sem nunca passar por 'Preparar esta semana'."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='diretivo-escrita-grade', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.user)
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.professor = Professor.objects.create(nome_completo='Gustavo Ramos Teixeira', ativo=True)
        self.turma_manha = Turma.objects.create(nome='1A', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.turma_tarde = Turma.objects.create(nome='6A', ano=2026, serie='6 Ano', ativa=True, turno='tarde')

        # Ativa e editando diferentes entre si e entre turnos, de proposito.
        self.semana_ativa_manha = date(2026, 8, 24)
        self.semana_ativa_tarde = date(2026, 8, 17)
        self.semana_editando_manha = date(2026, 8, 31)
        self.semana_editando_tarde = date(2026, 9, 7)
        self.semana_arbitraria = date(2026, 9, 14)  # nem ativa nem editando em nenhum turno

        self.configuracao = ConfiguracaoCronogramaLaboratorio.objects.create(
            data_inicio_cronograma_ativo=date(2020, 1, 6),
            data_fim_cronograma_ativo=date(2020, 1, 12),
            data_inicio_manha_ativo=self.semana_ativa_manha,
            data_fim_manha_ativo=self.semana_ativa_manha + timedelta(days=6),
            data_inicio_tarde_ativo=self.semana_ativa_tarde,
            data_fim_tarde_ativo=self.semana_ativa_tarde + timedelta(days=6),
            data_inicio_manha_editando=self.semana_editando_manha,
            data_inicio_tarde_editando=self.semana_editando_tarde,
        )

    def _turma(self, turno):
        return self.turma_manha if turno == 'manha' else self.turma_tarde

    def _post_grade(self, turno, data_semana, cliente=None, horario='3', disciplina='Nova aula', agendamento_id=''):
        cliente = cliente or self.client
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': turno})
        return cliente.post(f'{url}?data_inicio={data_semana.isoformat()}', {
            'agendamento_id': agendamento_id,
            'laboratorio_id': self.laboratorio.id,
            'data': data_semana.isoformat(),
            'horario': horario,
            'turno': turno,
            'professor': self.professor.id,
            'turma': self._turma(turno).id,
            'disciplina': disciplina,
            'observacao': '',
            'data_inicio': data_semana.isoformat(),
            'laboratorio_filtro': '',
            'scroll_y': '0',
        })

    def _get_grade(self, turno, data_semana):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': turno})
        return self.client.get(url, {'data_inicio': data_semana.isoformat()})

    # ---------------------------------------------------------------- MANHA

    def test_manha_semana_ativa_permite_post(self):
        response = self._post_grade('manha', self.semana_ativa_manha, disciplina='Aula na ativa')
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Aula na ativa', turno='manha').exists())
        self.assertRedirects(
            response,
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
            + f'?data_inicio={self.semana_ativa_manha.isoformat()}&scroll_y=0',
        )

    def test_manha_semana_editando_permite_post(self):
        self._post_grade('manha', self.semana_editando_manha, disciplina='Aula na editando')
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Aula na editando', turno='manha').exists())

    def test_manha_semana_arbitraria_recusa_post(self):
        response = self._post_grade('manha', self.semana_arbitraria, disciplina='Nao deveria salvar')
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Nao deveria salvar').exists())
        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))

    def test_manha_marcador_none_semana_arbitraria_recusa_post(self):
        ConfiguracaoCronogramaLaboratorio.objects.update(data_inicio_manha_editando=None)
        response = self._post_grade('manha', self.semana_arbitraria, disciplina='Nao deveria salvar 2')
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Nao deveria salvar 2').exists())
        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))

    def test_manha_nao_libera_post_com_semana_ativa_da_tarde(self):
        response = self._post_grade('manha', self.semana_ativa_tarde, disciplina='Vazando estado da tarde')
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Vazando estado da tarde').exists())
        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))

    def test_manha_nao_libera_post_com_semana_editando_da_tarde(self):
        response = self._post_grade('manha', self.semana_editando_tarde, disciplina='Vazando editando da tarde')
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Vazando editando da tarde').exists())
        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))

    # ---------------------------------------------------------------- TARDE

    def test_tarde_semana_ativa_permite_post(self):
        self._post_grade('tarde', self.semana_ativa_tarde, disciplina='Aula tarde na ativa')
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Aula tarde na ativa', turno='tarde').exists())

    def test_tarde_semana_editando_permite_post(self):
        self._post_grade('tarde', self.semana_editando_tarde, disciplina='Aula tarde na editando')
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Aula tarde na editando', turno='tarde').exists())

    def test_tarde_semana_arbitraria_recusa_post(self):
        response = self._post_grade('tarde', self.semana_arbitraria, disciplina='Nao deveria salvar tarde')
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Nao deveria salvar tarde').exists())
        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))

    def test_tarde_marcador_none_semana_arbitraria_recusa_post(self):
        ConfiguracaoCronogramaLaboratorio.objects.update(data_inicio_tarde_editando=None)
        response = self._post_grade('tarde', self.semana_arbitraria, disciplina='Nao deveria salvar tarde 2')
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Nao deveria salvar tarde 2').exists())
        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))

    def test_tarde_nao_libera_post_com_semana_ativa_da_manha(self):
        response = self._post_grade('tarde', self.semana_ativa_manha, disciplina='Vazando estado da manha')
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Vazando estado da manha').exists())
        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))

    def test_tarde_nao_libera_post_com_semana_editando_da_manha(self):
        response = self._post_grade('tarde', self.semana_editando_manha, disciplina='Vazando editando da manha')
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Vazando editando da manha').exists())
        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))

    # ------------------------------------------------------------ SEGURANCA

    def test_post_bloqueado_nao_cria_agendamentolab(self):
        total_antes = AgendamentoLab.objects.count()
        self._post_grade('manha', self.semana_arbitraria)
        self.assertEqual(AgendamentoLab.objects.count(), total_antes)

    def test_post_bloqueado_nao_altera_agendamento_existente(self):
        """Reproduz o cenario real da semana de 17/08: um AgendamentoLab ja
        existe numa semana que nao e ativa nem editando (dado legado/ja
        criado antes desta correcao) - tentar edita-lo por essa mesma tela
        deve ser recusado, sem alterar o registro."""
        existente = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=self.semana_arbitraria, horario='1', turno='manha',
            professor=self.professor, turma=self.turma_manha, disciplina='Original preservado',
            registrado_por=self.user,
        )
        response = self._post_grade(
            'manha', self.semana_arbitraria, horario='1',
            disciplina='Tentativa de alterar', agendamento_id=existente.id,
        )
        existente.refresh_from_db()
        self.assertEqual(existente.disciplina, 'Original preservado')
        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))

    def test_post_bloqueado_nao_altera_periodo_ativo(self):
        antes = (self.configuracao.data_inicio_manha_ativo, self.configuracao.data_fim_manha_ativo,
                  self.configuracao.data_inicio_tarde_ativo, self.configuracao.data_fim_tarde_ativo)
        self._post_grade('manha', self.semana_arbitraria)
        self.configuracao.refresh_from_db()
        depois = (self.configuracao.data_inicio_manha_ativo, self.configuracao.data_fim_manha_ativo,
                  self.configuracao.data_inicio_tarde_ativo, self.configuracao.data_fim_tarde_ativo)
        self.assertEqual(antes, depois)

    def test_post_bloqueado_nao_altera_editando(self):
        antes = (self.configuracao.data_inicio_manha_editando, self.configuracao.data_inicio_tarde_editando)
        self._post_grade('manha', self.semana_arbitraria)
        self.configuracao.refresh_from_db()
        depois = (self.configuracao.data_inicio_manha_editando, self.configuracao.data_inicio_tarde_editando)
        self.assertEqual(antes, depois)
        # nao "resolve" o bloqueio virando semana em edicao automaticamente
        self.assertNotEqual(self.configuracao.data_inicio_manha_editando, self.semana_arbitraria)

    def test_get_semana_arbitraria_continua_funcionando(self):
        response = self._get_grade('manha', self.semana_arbitraria)
        self.assertEqual(response.status_code, 200)

    def test_get_semana_arbitraria_mostra_visualizando_semana(self):
        response = self._get_grade('manha', self.semana_arbitraria)
        self.assertEqual(
            response.context['cronograma_titulo'],
            f'🔍 Visualizando Semana — Manhã · Início: {self.semana_arbitraria.strftime("%d/%m/%Y")}',
        )

    def test_post_manual_fabricado_direto_na_url_tambem_e_bloqueado(self):
        """Mesmo forjando o campo 'turno' no corpo do POST (turno_forcado
        continua vindo da URL, nunca do corpo), a semana arbitraria segue
        bloqueada."""
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        response = self.client.post(f'{url}?data_inicio={self.semana_arbitraria.isoformat()}', {
            'laboratorio_id': self.laboratorio.id,
            'data': self.semana_arbitraria.isoformat(),
            'horario': '2',
            'turno': 'tarde',  # forjado - nao deveria fazer diferenca
            'professor': self.professor.id,
            'turma': self.turma_manha.id,
            'disciplina': 'POST forjado',
            'observacao': '',
            'data_inicio': self.semana_arbitraria.isoformat(),
            'laboratorio_filtro': '',
            'scroll_y': '0',
        })
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='POST forjado').exists())
        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))

    def test_turno_todos_continua_indisponivel_na_rota_travada(self):
        response = self.client.post(
            '/laboratorios/cronograma/todos/',
            {'laboratorio_id': self.laboratorio.id, 'data': self.semana_ativa_manha.isoformat(), 'horario': '1'},
        )
        self.assertEqual(response.status_code, 404)

    # -------------------------------------------------------------- INTERFACE

    def test_controles_de_grade_disponiveis_na_semana_ativa(self):
        response = self._get_grade('manha', self.semana_ativa_manha)
        self.assertTrue(response.context['pode_editar_grade'])
        self.assertContains(response, 'Cadastrar')

    def test_controles_de_grade_disponiveis_na_semana_editando(self):
        response = self._get_grade('manha', self.semana_editando_manha)
        self.assertTrue(response.context['pode_editar_grade'])
        self.assertContains(response, 'Cadastrar')

    def test_controles_de_grade_indisponiveis_em_visualizando_semana(self):
        response = self._get_grade('manha', self.semana_arbitraria)
        self.assertFalse(response.context['pode_editar_grade'])
        # 'schedule-empty-action' tambem aparece na regra CSS do <style> da
        # pagina (sempre presente); a checagem real e pelo botao em si, que
        # so existe com essa combinacao exata de classes no elemento.
        self.assertNotContains(response, 'btn-outline-primary btn-sm schedule-empty-action')
        self.assertNotContains(response, 'data-bs-target="#agendamentoModal"')
        self.assertContains(response, '<span class="text-muted small">—</span>')

    def test_imprimir_pdf_e_excel_continuam_disponiveis_em_visualizando_semana(self):
        response = self._get_grade('manha', self.semana_arbitraria)
        self.assertContains(response, 'Imprimir / PDF')
        self.assertContains(response, 'Exportar para Excel')


class DiagnosticoCronogramaSemCartaoGenericoTests(TestCase):
    """Cobre a remocao do cartao generico 'Laboratorio / Agendamentos,
    professores e turmas / Ver / editar cronograma' do painel de diagnostico,
    preservando Editar manha, Editar tarde, Imprimir/PDF e Exportar Excel nas
    telas travadas por turno (sem reintroduzir a opcao Todos)."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='diretivo-sem-cartao', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.user)
        self.turma = Turma.objects.create(nome='1A', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.professor = Professor.objects.create(nome_completo='Camila Ribas', ativo=True)
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.semana = date(2026, 8, 24)
        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=self.semana, horario='1', turno='manha',
            professor=self.professor, turma=self.turma, disciplina='Aula',
            registrado_por=self.user,
        )
        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=self.semana, horario='2', turno='tarde',
            professor=self.professor, turma=self.turma, disciplina='Aula',
            registrado_por=self.user,
        )

    def test_cartao_generico_nao_aparece_mais_no_diagnostico(self):
        response = self.client.get(reverse('diagnostico_cronograma_laboratorios'))
        self.assertNotContains(response, 'Ver / editar cronograma')
        self.assertNotContains(response, 'Agendamentos, professores e turmas')

    def test_botao_cronograma_semanal_generico_nao_aparece_mais_no_cabecalho(self):
        """O link 'Cronograma semanal' (sem turno) some do cabecalho do
        diagnostico, mas a rota 'cronograma_semanal' continua existindo e
        funcionando por baixo (usada internamente por cronograma_semanal_turno
        e pelos redirects de manha/tarde) - so a porta de entrada generica na
        interface e removida."""
        response = self.client.get(reverse('diagnostico_cronograma_laboratorios'))
        # Texto exato do botao removido do cabecalho do card de diagnostico
        # (nao confundir com o item "Cronograma de Laboratorios" do menu
        # principal do site, que agora tambem aponta para o diagnostico -
        # ver test_menu_principal_cronograma_laboratorios_aponta_para_diagnostico).
        self.assertNotContains(response, 'Cronograma semanal')
        self.assertNotContains(response, '<i class="fas fa-calendar-days me-1"></i>Cronograma semanal')
        self.assertContains(response, 'Verificar cronograma pelo PDF')
        self.assertContains(response, reverse('verificar_horario_pdf_upload'))

        # A rota generica permanece como entrada de compatibilidade segura.
        resposta_rota_generica = self.client.get(reverse('cronograma_semanal'))
        self.assertRedirects(resposta_rota_generica, reverse('diagnostico_cronograma_laboratorios'))

    def test_menu_principal_cronograma_laboratorios_aponta_para_diagnostico(self):
        """O item 'Cronograma de Laboratorios' do menu principal (renderizado
        via base.html em qualquer pagina autenticada, incluindo a propria
        tela de diagnostico) deve continuar existindo, mas apontando para o
        Diagnostico do Cronograma - nunca mais direto para cronograma_semanal."""
        response = self.client.get(reverse('diagnostico_cronograma_laboratorios'))
        conteudo = response.content.decode()

        self.assertContains(response, 'Cronograma de Laborat&oacute;rios')

        url_diagnostico = reverse('diagnostico_cronograma_laboratorios')
        url_generica = reverse('cronograma_semanal')

        # O href da rota generica pura (sem sufixo de turno) nao aparece mais
        # em lugar nenhum da pagina - nem no menu, nem no restante do diagnostico.
        self.assertNotIn(f'href="{url_generica}"', conteudo)

        # O href do diagnostico aparece pelo menos duas vezes: uma no item
        # "Cronograma de Laboratorios" do menu e outra no item "Diagnostico
        # do Cronograma" (que ja apontava para la antes desta mudanca).
        self.assertGreaterEqual(conteudo.count(f'href="{url_diagnostico}"'), 2)

        # Editar manha/tarde continuam disponiveis a partir do diagnostico,
        # que agora e o unico caminho de entrada pela interface. Na Fase 2, a
        # preparacao de uma nova semana passou de um botao/modal generico
        # "Editar manha/tarde" para um botao dedicado por turno ("+ Criar
        # semana"), ja que agora pode haver varias semanas em edicao ao
        # mesmo tempo - cada uma com seu proprio link de edicao (ver
        # CartaoDiagnosticoExibeAtivoEEmEdicaoTests).
        self.assertContains(response, 'Criar semana — Manhã')
        self.assertContains(response, 'Criar semana — Tarde')

        # A URL generica e apenas uma entrada de compatibilidade e envia a
        # Equipe Diretiva para o diagnostico.
        resposta_rota_generica = self.client.get(url_generica)
        self.assertRedirects(resposta_rota_generica, url_diagnostico)

    def test_editar_manha_continua_disponivel_no_diagnostico(self):
        response = self.client.get(reverse('diagnostico_cronograma_laboratorios'))
        self.assertContains(response, 'Criar semana — Manhã')
        self.assertContains(response, 'modalCriarSemanaManha')

    def test_editar_tarde_continua_disponivel_no_diagnostico(self):
        response = self.client.get(reverse('diagnostico_cronograma_laboratorios'))
        self.assertContains(response, 'Criar semana — Tarde')
        self.assertContains(response, 'modalCriarSemanaTarde')

    def test_imprimir_pdf_disponivel_entrando_pela_manha(self):
        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}),
            {'data_inicio': self.semana.isoformat()},
        )
        self.assertContains(response, 'Imprimir / PDF')
        url_print = reverse('cronograma_print')
        self.assertContains(response, f'{url_print}?data_inicio={self.semana.isoformat()}&turno=manha')

    def test_imprimir_pdf_disponivel_entrando_pela_tarde(self):
        response = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'}),
            {'data_inicio': self.semana.isoformat()},
        )
        self.assertContains(response, 'Imprimir / PDF')
        url_print = reverse('cronograma_print')
        self.assertContains(response, f'{url_print}?data_inicio={self.semana.isoformat()}&turno=tarde')

    def test_exportar_excel_disponivel_nos_dois_turnos(self):
        url_excel = reverse('exportar_cronograma_excel')

        response_manha = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}),
            {'data_inicio': self.semana.isoformat()},
        )
        self.assertContains(response_manha, 'Exportar para Excel')
        self.assertContains(response_manha, f'{url_excel}?data_inicio={self.semana.isoformat()}&turno=manha')

        response_tarde = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'}),
            {'data_inicio': self.semana.isoformat()},
        )
        self.assertContains(response_tarde, 'Exportar para Excel')
        self.assertContains(response_tarde, f'{url_excel}?data_inicio={self.semana.isoformat()}&turno=tarde')

    def test_opcao_todos_continua_ausente_apos_a_mudanca(self):
        """A opcao 'Todos' de TURNO continua ausente (o filtro de laboratorio
        tem seu proprio 'Todos', legitimo, e nao deve ser confundido com o de
        turno - por isso o teste verifica a ausencia do <select name="turno">
        inteiro, nao apenas a palavra 'Todos')."""
        for turno in ('manha', 'tarde'):
            response = self.client.get(
                reverse('cronograma_semanal_turno', kwargs={'turno_forcado': turno}),
                {'data_inicio': self.semana.isoformat()},
            )
            self.assertNotContains(response, '<select name="turno"')
            self.assertContains(response, f'data-turno-travado="{turno}"')


class GestaoLaboratoriosSemAgendamentoDiretoTests(TestCase):
    """Cobre a remocao dos botoes 'Agendar Laboratorio' (Labs 1-5) e 'Ver
    Cronograma Semanal' da tela de Gestao de Laboratorios (/laboratorios/,
    view listar_laboratorios). Depois desta mudanca, a unica forma de criar
    ou editar AgendamentoLab pela interface e via Editar manha / Editar
    tarde (cronograma_semanal_turno) - 'Solicitar Emprestimo' fica de fora
    do escopo porque cria registros de Emprestimo, um model diferente."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='diretivo-gestao-labs', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.user)
        self.lab1 = Laboratorio.objects.create(nome='Lab. 01', tipo='fixo', equipamento='Desktop', ativo=True)
        Laboratorio.objects.create(nome='Lab. 02', tipo='fixo', equipamento='Notebook', ativo=True)
        Laboratorio.objects.create(nome='Lab. 03', tipo='fixo', equipamento='Chromebook', ativo=True)
        Laboratorio.objects.create(
            nome='Lab. 04', tipo='itinerante', equipamento='Tablets', quantidade_total=50, ativo=True,
        )
        Laboratorio.objects.create(
            nome='Lab. 05', tipo='itinerante', equipamento='Celulares', quantidade_total=30, ativo=True,
        )
        self.turma_manha = Turma.objects.create(nome='1A', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.turma_tarde = Turma.objects.create(nome='6A', ano=2026, serie='6 Ano', ativa=True, turno='tarde')
        self.professor = Professor.objects.create(nome_completo='Diego Nunes Farias', ativo=True)
        self.semana = date(2026, 8, 24)
        self.agendamento_manha = AgendamentoLab.objects.create(
            laboratorio=self.lab1, data=self.semana, horario='1', turno='manha',
            professor=self.professor, turma=self.turma_manha, disciplina='Matematica',
            registrado_por=self.user,
        )
        self.agendamento_tarde = AgendamentoLab.objects.create(
            laboratorio=self.lab1, data=self.semana, horario='1', turno='tarde',
            professor=self.professor, turma=self.turma_tarde, disciplina='Historia',
            registrado_por=self.user,
        )

    def test_agendar_laboratorio_nao_aparece_para_nenhum_laboratorio(self):
        response = self.client.get(reverse('listar_laboratorios'))
        self.assertNotContains(response, 'Agendar Laboratorio')
        self.assertNotContains(response, reverse('agendamento_lab', args=[self.lab1.id]))

    def test_ver_cronograma_semanal_nao_aparece_mais_na_gestao_de_laboratorios(self):
        response = self.client.get(reverse('listar_laboratorios'))
        self.assertNotContains(response, 'Ver Cronograma Semanal')

    def test_laboratorios_1_a_5_continuam_exibidos(self):
        response = self.client.get(reverse('listar_laboratorios'))
        for nome in ('Lab. 01', 'Lab. 02', 'Lab. 03', 'Lab. 04', 'Lab. 05'):
            self.assertContains(response, nome)

    def test_solicitar_emprestimo_continua_disponivel_fora_do_escopo(self):
        response = self.client.get(reverse('listar_laboratorios'))
        self.assertContains(response, 'Solicitar Emprestimo')

    def test_editar_manha_continua_permitindo_fluxo_normal_de_agendamento(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        response = self.client.post(url, {
            'laboratorio_id': self.lab1.id,
            'data': self.semana.isoformat(),
            'horario': '2',
            'turno': 'manha',
            'professor': self.professor.id,
            'turma': self.turma_manha.id,
            'disciplina': 'Fisica',
            'observacao': '',
            'data_inicio': self.semana.isoformat(),
            'laboratorio_filtro': '',
            'scroll_y': '',
        })
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Fisica', turno='manha').exists())

    def test_editar_tarde_continua_permitindo_fluxo_normal_de_agendamento(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'})
        response = self.client.post(url, {
            'laboratorio_id': self.lab1.id,
            'data': self.semana.isoformat(),
            'horario': '2',
            'turno': 'tarde',
            'professor': self.professor.id,
            'turma': self.turma_tarde.id,
            'disciplina': 'Geografia',
            'observacao': '',
            'data_inicio': self.semana.isoformat(),
            'laboratorio_filtro': '',
            'scroll_y': '',
        })
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Geografia', turno='tarde').exists())

    def test_manha_nao_consegue_editar_registro_da_tarde(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        self.client.post(url, {
            'agendamento_id': self.agendamento_tarde.id,
            'laboratorio_id': self.lab1.id,
            'data': self.semana.isoformat(),
            'horario': '1',
            'turno': 'manha',
            'professor': self.professor.id,
            'turma': self.turma_manha.id,
            'disciplina': 'Alterado indevidamente',
            'observacao': '',
            'data_inicio': self.semana.isoformat(),
            'laboratorio_filtro': '',
            'scroll_y': '',
        })
        self.agendamento_tarde.refresh_from_db()
        self.assertEqual(self.agendamento_tarde.disciplina, 'Historia')
        self.assertEqual(self.agendamento_tarde.turno, 'tarde')

    def test_tarde_nao_consegue_editar_registro_da_manha(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'})
        self.client.post(url, {
            'agendamento_id': self.agendamento_manha.id,
            'laboratorio_id': self.lab1.id,
            'data': self.semana.isoformat(),
            'horario': '1',
            'turno': 'tarde',
            'professor': self.professor.id,
            'turma': self.turma_tarde.id,
            'disciplina': 'Alterado indevidamente',
            'observacao': '',
            'data_inicio': self.semana.isoformat(),
            'laboratorio_filtro': '',
            'scroll_y': '',
        })
        self.agendamento_manha.refresh_from_db()
        self.assertEqual(self.agendamento_manha.disciplina, 'Matematica')
        self.assertEqual(self.agendamento_manha.turno, 'manha')

    def test_sem_regressao_em_imprimir_pdf_e_excel_apos_a_remocao(self):
        url_print = reverse('cronograma_print')
        url_excel = reverse('exportar_cronograma_excel')

        for turno in ('manha', 'tarde'):
            response = self.client.get(
                reverse('cronograma_semanal_turno', kwargs={'turno_forcado': turno}),
                {'data_inicio': self.semana.isoformat()},
            )
            self.assertContains(response, 'Imprimir / PDF')
            self.assertContains(response, 'Exportar para Excel')
            self.assertContains(response, f'{url_print}?data_inicio={self.semana.isoformat()}&turno={turno}')
            self.assertContains(response, f'{url_excel}?data_inicio={self.semana.isoformat()}&turno={turno}')


class HomeProfessorLaboratoriosTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='professor', password='senha-teste')
        self.outro_user = User.objects.create_user(username='outro-professor', password='senha-teste')
        self.professor = Professor.objects.create(
            usuario=self.user,
            nome_completo='Carlos Alberto Souza',
            ativo=True,
        )
        self.outro_professor = Professor.objects.create(
            usuario=self.outro_user,
            nome_completo='Marina Oliveira',
            ativo=True,
        )
        self.turma = Turma.objects.create(nome='3A', ano=2026, serie='3 Ano', ativa=True, turno='manha')
        self.laboratorio_1 = Laboratorio.objects.create(nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True)
        self.laboratorio_2 = Laboratorio.objects.create(nome='Laboratorio 2', tipo='fixo', equipamento='Computadores', ativo=True)
        self.semana_atual_inicio = timezone.localdate() - timedelta(days=timezone.localdate().weekday())

    def _agendar(self, professor, laboratorio, data, horario='1', disciplina='Fisica', observacao='', turno='manha'):
        return AgendamentoLab.objects.create(
            laboratorio=laboratorio,
            data=data,
            horario=horario,
            turno=turno,
            professor=professor,
            turma=self.turma,
            disciplina=disciplina,
            observacao=observacao,
            registrado_por=self.user,
        )

    def test_home_mostra_link_discreto_para_meus_horarios(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('home'))

        self.assertContains(response, 'Meus Horarios de Laboratorio')
        self.assertContains(response, reverse('meus_horarios_laboratorio'))
        self.assertNotContains(response, 'Nenhum horario de laboratorio encontrado.')

    def test_professor_logado_ve_apenas_proprios_agendamentos(self):
        self._agendar(
            self.professor,
            self.laboratorio_1,
            self.semana_atual_inicio,
            horario='1',
            disciplina='Fisica',
            observacao='Levar roteiro',
        )
        self._agendar(
            self.outro_professor,
            self.laboratorio_2,
            self.semana_atual_inicio,
            horario='2',
            disciplina='Quimica',
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse('meus_horarios_laboratorio'))

        self.assertContains(response, 'Meus Horarios de Laboratorio')
        self.assertContains(response, 'Laboratorio 1')
        self.assertContains(response, 'Fisica')
        self.assertContains(response, 'Levar roteiro')
        self.assertNotContains(response, 'Laboratorio 2')
        self.assertNotContains(response, 'Quimica')
        self.assertEqual(len(response.context['horarios_laboratorio_professor']['agendamentos']), 1)

    def test_meus_horarios_filtro_manha_funciona(self):
        self._agendar(self.professor, self.laboratorio_1, self.semana_atual_inicio, disciplina='Fisica', turno='manha')
        self._agendar(self.professor, self.laboratorio_2, self.semana_atual_inicio, horario='2', disciplina='Robotica', turno='tarde')
        self.client.force_login(self.user)

        response = self.client.get(reverse('meus_horarios_laboratorio'), {'turno': 'manha'})

        self.assertContains(response, 'Fisica')
        self.assertContains(response, 'Manha')
        self.assertNotContains(response, 'Robotica')
        self.assertNotContains(response, 'data-turno-separator="manha"')
        self.assertNotContains(response, 'data-turno-separator="tarde"')
        self.assertEqual(response.context['horarios_laboratorio_professor']['turno_filtro'], 'manha')
        self.assertEqual(len(response.context['horarios_laboratorio_professor']['agendamentos']), 1)

    def test_meus_horarios_filtro_tarde_funciona(self):
        self._agendar(self.professor, self.laboratorio_1, self.semana_atual_inicio, disciplina='Fisica', turno='manha')
        self._agendar(self.professor, self.laboratorio_2, self.semana_atual_inicio, horario='2', disciplina='Robotica', turno='tarde')
        self.client.force_login(self.user)

        response = self.client.get(reverse('meus_horarios_laboratorio'), {'turno': 'tarde'})

        self.assertContains(response, 'Robotica')
        self.assertContains(response, 'Tarde')
        self.assertNotContains(response, 'Fisica')
        self.assertNotContains(response, 'data-turno-separator="manha"')
        self.assertNotContains(response, 'data-turno-separator="tarde"')
        self.assertEqual(response.context['horarios_laboratorio_professor']['turno_filtro'], 'tarde')
        self.assertEqual(len(response.context['horarios_laboratorio_professor']['agendamentos']), 1)

    def test_meus_horarios_filtro_todos_funciona(self):
        self._agendar(self.professor, self.laboratorio_1, self.semana_atual_inicio, disciplina='Fisica', turno='manha')
        self._agendar(self.professor, self.laboratorio_2, self.semana_atual_inicio, horario='2', disciplina='Robotica', turno='tarde')
        self.client.force_login(self.user)

        response = self.client.get(reverse('meus_horarios_laboratorio'))

        self.assertContains(response, 'Fisica')
        self.assertContains(response, 'Robotica')
        self.assertContains(response, 'data-turno-separator="manha"')
        self.assertContains(response, 'data-turno-separator="tarde"')
        conteudo = response.content.decode()
        self.assertLess(conteudo.index('MANHA'), conteudo.index('Fisica'))
        self.assertLess(conteudo.index('Fisica'), conteudo.index('TARDE'))
        self.assertLess(conteudo.index('TARDE'), conteudo.index('Robotica'))
        self.assertEqual(response.context['horarios_laboratorio_professor']['turno_filtro'], '')
        self.assertEqual(len(response.context['horarios_laboratorio_professor']['agendamentos']), 2)

    def test_meus_horarios_filtro_sem_resultado_mostra_mensagem_do_turno(self):
        self._agendar(self.professor, self.laboratorio_1, self.semana_atual_inicio, disciplina='Fisica', turno='manha')
        self.client.force_login(self.user)

        response = self.client.get(reverse('meus_horarios_laboratorio'), {'turno': 'tarde'})

        self.assertContains(response, 'Nenhum horario encontrado para este turno.')
        self.assertNotContains(response, 'Fisica')
        self.assertEqual(response.context['horarios_laboratorio_professor']['agendamentos'], [])

    def test_meus_horarios_usa_semana_atual_quando_tem_agendamento(self):
        data_atual = self.semana_atual_inicio + timedelta(days=1)
        data_antiga = self.semana_atual_inicio - timedelta(days=14)
        self._agendar(self.professor, self.laboratorio_1, data_atual, disciplina='Biologia')
        self._agendar(self.professor, self.laboratorio_2, data_antiga, horario='2', disciplina='Geografia')
        self.client.force_login(self.user)

        response = self.client.get(reverse('meus_horarios_laboratorio'))

        self.assertContains(response, 'Biologia')
        self.assertNotContains(response, 'Geografia')
        self.assertEqual(response.context['horarios_laboratorio_professor']['semana_inicio'], self.semana_atual_inicio)

    def test_meus_horarios_sem_semana_atual_pega_semana_mais_recente(self):
        data_antiga = self.semana_atual_inicio - timedelta(days=14)
        data_recente = self.semana_atual_inicio - timedelta(days=7)
        self._agendar(self.professor, self.laboratorio_1, data_antiga, disciplina='Artes')
        self._agendar(self.professor, self.laboratorio_2, data_recente, horario='2', disciplina='Robotica')
        self.client.force_login(self.user)

        response = self.client.get(reverse('meus_horarios_laboratorio'))

        self.assertContains(response, 'Robotica')
        self.assertNotContains(response, 'Artes')
        self.assertEqual(
            response.context['horarios_laboratorio_professor']['semana_inicio'],
            data_recente - timedelta(days=data_recente.weekday()),
        )

    def test_meus_horarios_sem_agendamento_mostra_mensagem(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('meus_horarios_laboratorio'))

        self.assertContains(response, 'Nenhum horario de laboratorio encontrado.')
        self.assertEqual(response.context['horarios_laboratorio_professor']['agendamentos'], [])


class MeusHorariosUsaVersaoAtivaIndependenteDeDataTests(TestCase):
    """Corrige o bug encontrado ao investigar as 3 falhas antigas de
    HomeProfessorLaboratoriosTests: 'Meus Horarios' deve sempre usar a
    versao ATIVA (status='ativa' em SemanaCronogramaLaboratorio) de cada
    turno - nunca uma janela calculada a partir de date.today()/
    timezone.now(), e nunca uma unica janela combinada para manha+tarde
    quando eles tem semanas-base diferentes. Datas fixas e bem distantes
    da data real do servidor, de proposito, para nao depender dela."""

    def setUp(self):
        self.user = User.objects.create_user(username='professor-versao-ativa', password='senha-teste')
        self.outro_user = User.objects.create_user(username='outro-professor-versao-ativa', password='senha-teste')
        self.professor = Professor.objects.create(
            usuario=self.user, nome_completo='Renata Duarte Lima', ativo=True,
        )
        self.outro_professor = Professor.objects.create(
            usuario=self.outro_user, nome_completo='Vinicius Prado Costa', ativo=True,
        )
        self.turma_manha = Turma.objects.create(nome='2A', ano=2026, serie='2 Ano', ativa=True, turno='manha')
        self.turma_tarde = Turma.objects.create(nome='7A', ano=2026, serie='7 Ano', ativa=True, turno='tarde')
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.client.force_login(self.user)
        # Semanas-base bem distantes do "hoje" real do servidor, e
        # deliberadamente diferentes entre manha e tarde (turnos
        # independentes - nao ha garantia de que compartilhem a mesma
        # semana-base).
        self.data_base_manha = date(2020, 1, 6)   # segunda-feira
        self.data_base_tarde = date(2020, 3, 2)   # segunda-feira, semana diferente

    def _agendar(self, professor, data, horario='1', disciplina='Matematica', turno='manha', turma=None, laboratorio=None):
        return AgendamentoLab.objects.create(
            laboratorio=laboratorio or self.laboratorio, data=data, horario=horario, turno=turno,
            professor=professor,
            turma=turma or (self.turma_manha if turno == 'manha' else self.turma_tarde),
            disciplina=disciplina, registrado_por=self.user,
        )

    def test_versao_ativa_antiga_continua_sendo_a_ativa(self):
        dias_desde_a_base = (date.today() - self.data_base_manha).days
        self.assertGreater(dias_desde_a_base, 7, 'o cenario testado precisa estar "muito depois" da semana-base')

        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=self.data_base_manha, status='ativa',
        )
        self._agendar(self.professor, self.data_base_manha, disciplina='Fisica Antiga')

        response = self.client.get(reverse('meus_horarios_laboratorio'), {'turno': 'manha'})

        self.assertContains(response, 'Fisica Antiga')
        self.assertEqual(
            response.context['horarios_laboratorio_professor']['semana_inicio'], self.data_base_manha,
        )

    def test_professor_ve_so_seus_proprios_horarios(self):
        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=self.data_base_manha, status='ativa',
        )
        self._agendar(self.professor, self.data_base_manha, disciplina='Do professor')
        self._agendar(self.outro_professor, self.data_base_manha, horario='2', disciplina='Do outro professor')

        response = self.client.get(reverse('meus_horarios_laboratorio'), {'turno': 'manha'})

        self.assertContains(response, 'Do professor')
        self.assertNotContains(response, 'Do outro professor')
        self.assertEqual(len(response.context['horarios_laboratorio_professor']['agendamentos']), 1)

    def test_manha_usa_ativa_da_manha_e_tarde_usa_ativa_da_tarde(self):
        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=self.data_base_manha, status='ativa',
        )
        SemanaCronogramaLaboratorio.objects.create(
            turno='tarde', data_inicio=self.data_base_tarde, status='ativa',
        )
        self._agendar(self.professor, self.data_base_manha, disciplina='Manha ativa', turno='manha')
        self._agendar(self.professor, self.data_base_tarde, disciplina='Tarde ativa', turno='tarde', horario='2')

        resposta_todos = self.client.get(reverse('meus_horarios_laboratorio'))
        self.assertContains(resposta_todos, 'Manha ativa')
        self.assertContains(resposta_todos, 'Tarde ativa')

        resposta_manha = self.client.get(reverse('meus_horarios_laboratorio'), {'turno': 'manha'})
        self.assertContains(resposta_manha, 'Manha ativa')
        self.assertNotContains(resposta_manha, 'Tarde ativa')
        self.assertEqual(
            resposta_manha.context['horarios_laboratorio_professor']['semana_inicio'], self.data_base_manha,
        )

        resposta_tarde = self.client.get(reverse('meus_horarios_laboratorio'), {'turno': 'tarde'})
        self.assertContains(resposta_tarde, 'Tarde ativa')
        self.assertNotContains(resposta_tarde, 'Manha ativa')
        self.assertEqual(
            resposta_tarde.context['horarios_laboratorio_professor']['semana_inicio'], self.data_base_tarde,
        )

    def test_ativar_nova_versao_faz_pagina_passar_a_usar_a_nova(self):
        semana_antiga = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=self.data_base_manha, status='ativa',
        )
        self._agendar(self.professor, self.data_base_manha, disciplina='Versao antiga')

        nova_data_base = date(2020, 2, 3)
        semana_nova = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=nova_data_base, status='editando',
        )
        self._agendar(self.professor, nova_data_base, disciplina='Versao nova')

        resposta_antes = self.client.get(reverse('meus_horarios_laboratorio'), {'turno': 'manha'})
        self.assertContains(resposta_antes, 'Versao antiga')
        self.assertNotContains(resposta_antes, 'Versao nova')

        diretivo = User.objects.create_user(
            username='diretivo-ativa-versao', password='senha-teste', is_superuser=True,
        )
        cliente_diretivo = Client()
        cliente_diretivo.force_login(diretivo)
        cliente_diretivo.post(reverse('ativar_cronograma_laboratorio'), {'semana_id': semana_nova.id})

        resposta_depois = self.client.get(reverse('meus_horarios_laboratorio'), {'turno': 'manha'})
        self.assertContains(resposta_depois, 'Versao nova')
        self.assertNotContains(resposta_depois, 'Versao antiga')
        self.assertEqual(
            resposta_depois.context['horarios_laboratorio_professor']['semana_inicio'], nova_data_base,
        )

        semana_antiga.refresh_from_db()
        self.assertEqual(semana_antiga.status, 'historica')

    def test_semana_editando_nao_substitui_a_ativa(self):
        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=self.data_base_manha, status='ativa',
        )
        self._agendar(self.professor, self.data_base_manha, disciplina='Ativa de verdade')

        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2020, 2, 3), status='editando',
        )
        self._agendar(self.professor, date(2020, 2, 3), disciplina='Em edicao nao deveria aparecer')

        response = self.client.get(reverse('meus_horarios_laboratorio'), {'turno': 'manha'})

        self.assertContains(response, 'Ativa de verdade')
        self.assertNotContains(response, 'Em edicao nao deveria aparecer')

    def test_semana_historica_nao_substitui_a_ativa(self):
        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=self.data_base_manha, status='ativa',
        )
        self._agendar(self.professor, self.data_base_manha, disciplina='Ativa de verdade')

        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2019, 12, 2), status='historica',
        )
        self._agendar(self.professor, date(2019, 12, 2), disciplina='Historica nao deveria aparecer')

        response = self.client.get(reverse('meus_horarios_laboratorio'), {'turno': 'manha'})

        self.assertContains(response, 'Ativa de verdade')
        self.assertNotContains(response, 'Historica nao deveria aparecer')

    def test_semana_cancelada_nao_substitui_a_ativa(self):
        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=self.data_base_manha, status='ativa',
        )
        self._agendar(self.professor, self.data_base_manha, disciplina='Ativa de verdade')

        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2020, 4, 6), status='cancelada',
        )
        self._agendar(self.professor, date(2020, 4, 6), disciplina='Cancelada nao deveria aparecer')

        response = self.client.get(reverse('meus_horarios_laboratorio'), {'turno': 'manha'})

        self.assertContains(response, 'Ativa de verdade')
        self.assertNotContains(response, 'Cancelada nao deveria aparecer')

    def test_comportamento_nao_depende_da_data_corrente_do_servidor(self):
        """Confirma por inspecao estatica do source que as funcoes que
        decidem a versao ativa nao comparam nada contra
        date.today()/timezone.now()/datetime.now(), reforcando em codigo a
        garantia ja auditada manualmente - e tambem confirma
        funcionalmente que uma semana-base de anos atras continua sendo
        usada normalmente."""
        import inspect
        from apps.views import laboratorios as views_laboratorios

        for funcao in (
            views_laboratorios._horarios_laboratorio_professor_obj,
            views_laboratorios.obter_cronograma_ativo,
            views_laboratorios._semana_ativa_obj,
        ):
            codigo = inspect.getsource(funcao)
            self.assertNotIn('date.today()', codigo, f'{funcao.__name__} nao deveria usar date.today()')
            self.assertNotIn('timezone.now()', codigo, f'{funcao.__name__} nao deveria usar timezone.now()')
            self.assertNotIn('datetime.now()', codigo, f'{funcao.__name__} nao deveria usar datetime.now()')

        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=self.data_base_manha, status='ativa',
        )
        self._agendar(self.professor, self.data_base_manha, disciplina='Independente da data atual')

        response = self.client.get(reverse('meus_horarios_laboratorio'), {'turno': 'manha'})
        self.assertContains(response, 'Independente da data atual')


class RotaGenericaCronogramaEExclusaoSeguraTests(TestCase):
    def setUp(self):
        self.equipe = User.objects.create_user(
            username='equipe-rota-generica', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.equipe)
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio Exclusao Segura', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.professor = Professor.objects.create(nome_completo='Professor Exclusao Segura', ativo=True)
        self.turma_manha = Turma.objects.create(
            nome='3A', ano=2026, serie='3 Ano', ativa=True, turno='manha',
        )
        self.turma_tarde = Turma.objects.create(
            nome='8A', ano=2026, serie='8 Ano', ativa=True, turno='tarde',
        )

    def _semana(self, status, turno='manha', data_inicio=date(2026, 8, 24)):
        return SemanaCronogramaLaboratorio.objects.create(
            turno=turno, data_inicio=data_inicio, status=status,
        )

    def _agendamento(self, semana=None, turno='manha', disciplina='Aula protegida'):
        data_agendamento = semana.data_inicio if semana else date(2026, 8, 24)
        return AgendamentoLab.objects.create(
            laboratorio=self.laboratorio,
            data=data_agendamento,
            horario='1',
            turno=turno,
            professor=self.professor,
            turma=self.turma_manha if turno == 'manha' else self.turma_tarde,
            disciplina=disciplina,
            registrado_por=self.equipe,
            semana=semana,
        )

    def _post_exclusao(self, agendamento):
        return self.client.post(reverse('excluir_agendamento', args=[agendamento.id]), {
            'data_inicio': agendamento.data.isoformat(),
            'turno': agendamento.turno,
            'turno_bloqueado': agendamento.turno,
            'laboratorio': str(self.laboratorio.id),
            'scroll_y': '120',
        })

    def _post_generico(self, agendamento_id='', disciplina='Tentativa generica'):
        return self.client.post(reverse('cronograma_semanal'), {
            'agendamento_id': str(agendamento_id) if agendamento_id else '',
            'laboratorio_id': self.laboratorio.id,
            'data': '2026-08-24',
            'horario': '1',
            'turno': 'manha',
            'professor': self.professor.id,
            'turma': self.turma_manha.id,
            'disciplina': disciplina,
            'observacao': '',
            'data_inicio': '2026-08-24',
            'turno_filtro': 'manha',
            'laboratorio_filtro': '',
            'scroll_y': '',
        })

    def test_bookmark_manha_redireciona_para_rota_travada_preservando_filtros(self):
        response = self.client.get(reverse('cronograma_semanal'), {
            'turno': 'manha', 'data_inicio': '2026-08-24',
            'laboratorio': str(self.laboratorio.id), 'scroll_y': '90',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
            + f'?data_inicio=2026-08-24&laboratorio={self.laboratorio.id}&scroll_y=90',
        )

    def test_bookmark_tarde_redireciona_para_rota_travada(self):
        response = self.client.get(reverse('cronograma_semanal'), {'turno': 'tarde'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'}),
        )

    def test_get_generico_sem_turno_redireciona_equipe_para_diagnostico(self):
        response = self.client.get(reverse('cronograma_semanal'))

        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))

    def test_get_generico_sem_turno_redireciona_consulta_para_tela_somente_leitura(self):
        grupo = Group.objects.create(name='Consulta Cronograma')
        usuario = User.objects.create_user(username='consulta-rota-generica', password='senha-teste')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        response = self.client.get(reverse('cronograma_semanal'))

        self.assertRedirects(response, reverse('consulta_cronograma_laboratorios'))

    def test_post_generico_nao_cria_agendamento(self):
        semana = self._semana('ativa')

        response = self._post_generico()

        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Tentativa generica').exists())
        self.assertTrue(SemanaCronogramaLaboratorio.objects.filter(id=semana.id, status='ativa').exists())

    def test_post_generico_nao_altera_nem_exclui_agendamento(self):
        agendamento = self._agendamento(self._semana('ativa'), disciplina='Original preservado')

        self._post_generico(agendamento.id, disciplina='Alteracao bloqueada')

        agendamento.refresh_from_db()
        self.assertEqual(agendamento.disciplina, 'Original preservado')
        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())

    def test_get_excluir_agendamento_nunca_exclui(self):
        agendamento = self._agendamento(self._semana('ativa'))

        response = self.client.get(reverse('excluir_agendamento', args=[agendamento.id]))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())

    def test_post_autorizado_exclui_agendamento_de_semana_ativa(self):
        agendamento = self._agendamento(self._semana('ativa'))

        response = self._post_exclusao(agendamento)

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AgendamentoLab.objects.filter(id=agendamento.id).exists())

    def test_post_autorizado_exclui_agendamento_de_semana_editando(self):
        agendamento = self._agendamento(self._semana('editando'))

        self._post_exclusao(agendamento)

        self.assertFalse(AgendamentoLab.objects.filter(id=agendamento.id).exists())

    def test_post_nao_exclui_agendamento_de_semana_historica(self):
        agendamento = self._agendamento(self._semana('historica'))

        response = self._post_exclusao(agendamento)

        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))
        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())

    def test_post_nao_exclui_agendamento_de_semana_cancelada(self):
        agendamento = self._agendamento(self._semana('cancelada'))

        self._post_exclusao(agendamento)

        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())

    def test_post_nao_exclui_sem_associacao_de_semana(self):
        agendamento = self._agendamento()

        self._post_exclusao(agendamento)

        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())

    def test_post_nao_exclui_com_associacao_de_turno_inconsistente(self):
        agendamento = self._agendamento(self._semana('ativa', turno='manha'), turno='tarde')

        self._post_exclusao(agendamento)

        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())

    def test_consulta_cronograma_nao_exclui_por_post_direto(self):
        agendamento = self._agendamento(self._semana('ativa'))
        grupo = Group.objects.create(name='Consulta Cronograma')
        usuario = User.objects.create_user(username='consulta-nao-exclui', password='senha-teste')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        response = self._post_exclusao(agendamento)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())

    def test_rotas_especificas_pdf_e_excel_continuam_funcionando(self):
        semana_manha = self._semana('ativa', turno='manha')
        semana_tarde = self._semana('ativa', turno='tarde')
        self._agendamento(semana_manha, turno='manha', disciplina='Manha preservada')
        self._agendamento(semana_tarde, turno='tarde', disciplina='Tarde preservada')

        for turno, disciplina in (('manha', 'Manha preservada'), ('tarde', 'Tarde preservada')):
            url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': turno})
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, disciplina)

        response_pdf = self.client.get(reverse('cronograma_print'), {
            'turno': 'manha', 'data_inicio': semana_manha.data_inicio.isoformat(),
        })
        response_excel = self.client.get(reverse('exportar_cronograma_excel'), {
            'turno': 'tarde', 'data_inicio': semana_tarde.data_inicio.isoformat(),
        })
        self.assertEqual(response_pdf.status_code, 200)
        self.assertContains(response_pdf, 'Manha preservada')
        self.assertEqual(response_excel.status_code, 200)
        self.assertEqual(
            response_excel['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )


@override_settings(CONSULTA_LABORATORIO_EM_MANUTENCAO=False)
class ConsultaPublicaProfessorObservacaoTests(TestCase):
    def setUp(self):
        self.registrador = User.objects.create_user(username='registrador', password='senha-teste')
        self.turma = Turma.objects.create(nome='1A', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.laboratorio = Laboratorio.objects.create(nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True)
        self.professor = Professor.objects.create(nome_completo='Joana Pereira', ativo=True)
        self.data_agendamento = date(2026, 5, 18)
        ConfiguracaoCronogramaLaboratorio.objects.create(
            data_inicio_cronograma_ativo=self.data_agendamento - timedelta(days=self.data_agendamento.weekday()),
            data_fim_cronograma_ativo=self.data_agendamento - timedelta(days=self.data_agendamento.weekday()) + timedelta(days=6),
        )

    def _agendar(self, professor, disciplina, observacao, horario='1'):
        return AgendamentoLab.objects.create(
            laboratorio=self.laboratorio,
            data=self.data_agendamento,
            horario=horario,
            turno='manha',
            professor=professor,
            turma=self.turma,
            disciplina=disciplina,
            observacao=observacao,
            registrado_por=self.registrador,
        )

    def _consultar(self):
        return self.client.get(
            reverse('consulta_publica_professor_laboratorio'),
            {'professor_id': str(self.professor.id)},
        )

    def test_observacao_aparece_quando_preenchida(self):
        self._agendar(self.professor, 'Matematica', 'Levar calculadora')

        response = self._consultar()

        self.assertContains(response, 'Observação:')
        self.assertContains(response, 'Levar calculadora')

    def test_observacao_nao_aparece_quando_vazia(self):
        self._agendar(self.professor, 'Matematica', '')

        response = self._consultar()

        self.assertNotContains(response, 'Observação:')

    def test_observacao_nao_aparece_quando_apenas_espacos(self):
        self._agendar(self.professor, 'Matematica', '   \n  ')

        response = self._consultar()

        self.assertNotContains(response, 'Observação:')

    def test_observacao_pertence_ao_agendamento_correto(self):
        outro_professor = Professor.objects.create(nome_completo='Ricardo Alves', ativo=True)
        self._agendar(self.professor, 'Matematica', 'Levar calculadora', horario='1')
        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio,
            data=self.data_agendamento,
            horario='2',
            turno='manha',
            professor=outro_professor,
            turma=self.turma,
            disciplina='Historia',
            observacao='Levar mapa',
            registrado_por=self.registrador,
        )

        response = self._consultar()

        self.assertContains(response, 'Levar calculadora')
        self.assertNotContains(response, 'Levar mapa')

    def test_observacao_preserva_quebras_de_linha_e_escapa_html(self):
        self._agendar(self.professor, 'Matematica', 'Linha 1\nLinha 2 <script>alert(1)</script>')

        response = self._consultar()
        conteudo = response.content.decode()

        self.assertIn('Linha 1<br>Linha 2 &lt;script&gt;alert(1)&lt;/script&gt;', conteudo)
        self.assertNotIn('<script>alert(1)</script>', conteudo)


class ConsultaPublicaLaboratorioManutencaoTests(TestCase):
    """Modo manutencao (CONSULTA_LABORATORIO_EM_MANUTENCAO) da consulta publica de laboratorios.

    So a pagina publica /laboratorios/consulta-professor/ deve mudar de comportamento;
    nada mais (admin, cronograma, verificacao por PDF, outras rotas publicas, dados) pode ser afetado.
    """

    def setUp(self):
        self.registrador = User.objects.create_user(username='registrador-manutencao', password='senha-teste')
        self.turma = Turma.objects.create(nome='1A-manut', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.laboratorio = Laboratorio.objects.create(nome='Laboratorio Manut', tipo='fixo', equipamento='Computadores', ativo=True)
        self.professor = Professor.objects.create(nome_completo='Professor Manutencao', ativo=True)
        self.data_agendamento = date(2026, 5, 18)
        self.configuracao = ConfiguracaoCronogramaLaboratorio.objects.create(
            data_inicio_cronograma_ativo=self.data_agendamento - timedelta(days=self.data_agendamento.weekday()),
            data_fim_cronograma_ativo=self.data_agendamento - timedelta(days=self.data_agendamento.weekday()) + timedelta(days=6),
        )
        self.agendamento = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio,
            data=self.data_agendamento,
            horario='1',
            turno='manha',
            professor=self.professor,
            turma=self.turma,
            disciplina='Matematica',
            registrado_por=self.registrador,
        )

    @override_settings(CONSULTA_LABORATORIO_EM_MANUTENCAO=True)
    def test_pagina_publica_mostra_mensagem_de_manutencao_com_status_200(self):
        response = self.client.get(reverse('consulta_publica_professor_laboratorio'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Sistema em manutenção')
        self.assertContains(
            response,
            'A consulta de horários dos laboratórios está temporariamente indisponível para atualização.',
        )

    @override_settings(CONSULTA_LABORATORIO_EM_MANUTENCAO=True)
    def test_usuario_nao_autenticado_ve_pagina_de_manutencao(self):
        # self.client nunca foi autenticado neste teste - simula visitante publico.
        response = self.client.get(
            reverse('consulta_publica_professor_laboratorio'),
            {'professor_id': str(self.professor.id)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Sistema em manutenção')

    @override_settings(CONSULTA_LABORATORIO_EM_MANUTENCAO=True)
    def test_nenhum_horario_de_professor_e_exibido_durante_manutencao(self):
        response = self.client.get(
            reverse('consulta_publica_professor_laboratorio'),
            {'professor_id': str(self.professor.id)},
        )

        self.assertNotContains(response, 'Professor Manutencao')
        self.assertNotContains(response, 'Matematica')
        self.assertNotContains(response, 'consultaProfessorForm')

    @override_settings(CONSULTA_LABORATORIO_EM_MANUTENCAO=True)
    def test_manutencao_nao_altera_agendamentos_nem_configuracao_de_cronograma(self):
        antes_agendamentos = list(AgendamentoLab.objects.values('id', 'turma_id', 'data', 'horario', 'turno'))
        antes_config = list(ConfiguracaoCronogramaLaboratorio.objects.values())

        self.client.get(reverse('consulta_publica_professor_laboratorio'), {'professor_id': str(self.professor.id)})

        depois_agendamentos = list(AgendamentoLab.objects.values('id', 'turma_id', 'data', 'horario', 'turno'))
        depois_config = list(ConfiguracaoCronogramaLaboratorio.objects.values())
        self.assertEqual(antes_agendamentos, depois_agendamentos)
        self.assertEqual(antes_config, depois_config)

    @override_settings(CONSULTA_LABORATORIO_EM_MANUTENCAO=True)
    def test_outras_rotas_publicas_de_laboratorio_continuam_funcionando(self):
        response_autocomplete = self.client.get(
            reverse('autocomplete_professores_laboratorio'), {'q': 'Professor'},
        )
        response_versao = self.client.get(
            reverse('versao_horario_professor_laboratorio_publico'), {'professor_id': str(self.professor.id)},
        )

        self.assertEqual(response_autocomplete.status_code, 200)
        self.assertIn('resultados', response_autocomplete.json())
        self.assertEqual(response_versao.status_code, 200)
        self.assertIn('versao', response_versao.json())

    @override_settings(CONSULTA_LABORATORIO_EM_MANUTENCAO=True)
    def test_area_administrativa_e_cronograma_continuam_funcionando(self):
        grupo_equipe = Group.objects.create(name='Equipe Diretiva')
        admin = User.objects.create_user(username='diretivo-manutencao', password='senha-teste')
        admin.groups.add(grupo_equipe)
        self.client.force_login(admin)

        response_diagnostico = self.client.get(reverse('diagnostico_cronograma_laboratorios'))
        response_cronograma = self.client.get(reverse('cronograma_semanal'))
        response_verificar_pdf = self.client.get(reverse('verificar_horario_pdf_upload'))

        self.assertEqual(response_diagnostico.status_code, 200)
        self.assertRedirects(response_cronograma, reverse('diagnostico_cronograma_laboratorios'))
        self.assertEqual(response_verificar_pdf.status_code, 200)

    @override_settings(CONSULTA_LABORATORIO_EM_MANUTENCAO=False)
    def test_flag_desligada_executa_consulta_normalmente(self):
        response = self.client.get(
            reverse('consulta_publica_professor_laboratorio'),
            {'professor_id': str(self.professor.id)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Sistema em manutenção')
        self.assertContains(response, 'consultaProfessorForm')


@override_settings(CONSULTA_LABORATORIO_EM_MANUTENCAO=False)
class VersaoHorarioProfessorLaboratorioPublicoTests(TestCase):
    def setUp(self):
        self.registrador = User.objects.create_user(username='registrador-versao', password='senha-teste')
        self.turma = Turma.objects.create(nome='1B', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.laboratorio = Laboratorio.objects.create(nome='Laboratorio 2', tipo='fixo', equipamento='Computadores', ativo=True)
        self.professor = Professor.objects.create(nome_completo='Carla Souza', ativo=True)
        self.data_agendamento = date(2026, 5, 18)
        self.semana_inicio = self.data_agendamento - timedelta(days=self.data_agendamento.weekday())
        self.semana_fim = self.semana_inicio + timedelta(days=6)
        self.configuracao = ConfiguracaoCronogramaLaboratorio.objects.create(
            data_inicio_cronograma_ativo=self.semana_inicio,
            data_fim_cronograma_ativo=self.semana_fim,
        )

    def _agendar(self, professor, disciplina='Matematica', horario='1'):
        return AgendamentoLab.objects.create(
            laboratorio=self.laboratorio,
            data=self.data_agendamento,
            horario=horario,
            turno='manha',
            professor=professor,
            turma=self.turma,
            disciplina=disciplina,
            observacao='',
            registrado_por=self.registrador,
        )

    def _consultar_versao(self, professor_id):
        return self.client.get(
            reverse('versao_horario_professor_laboratorio_publico'),
            {'professor_id': str(professor_id)},
        )

    def test_versao_muda_quando_agendamento_e_criado(self):
        versao_antes = self._consultar_versao(self.professor.id).json()['versao']

        self._agendar(self.professor)

        versao_depois = self._consultar_versao(self.professor.id).json()['versao']

        self.assertNotEqual(versao_antes, versao_depois)

    def test_versao_muda_quando_agendamento_e_editado(self):
        agendamento = self._agendar(self.professor)
        versao_antes = self._consultar_versao(self.professor.id).json()['versao']

        agendamento.disciplina = 'Historia'
        agendamento.save()

        versao_depois = self._consultar_versao(self.professor.id).json()['versao']

        self.assertNotEqual(versao_antes, versao_depois)

    def test_versao_muda_quando_agendamento_e_excluido(self):
        agendamento = self._agendar(self.professor)
        versao_antes = self._consultar_versao(self.professor.id).json()['versao']

        agendamento.delete()

        versao_depois = self._consultar_versao(self.professor.id).json()['versao']

        self.assertNotEqual(versao_antes, versao_depois)

    def test_versao_nao_muda_quando_agendamento_de_outro_professor_muda(self):
        outro_professor = Professor.objects.create(nome_completo='Ricardo Alves', ativo=True)
        versao_antes = self._consultar_versao(self.professor.id).json()['versao']

        self._agendar(outro_professor, horario='2')

        versao_depois = self._consultar_versao(self.professor.id).json()['versao']

        self.assertEqual(versao_antes, versao_depois)

    def test_versao_muda_quando_semana_ativa_do_cronograma_muda(self):
        self._agendar(self.professor)
        versao_antes = self._consultar_versao(self.professor.id).json()['versao']

        self.configuracao.data_inicio_cronograma_ativo = self.semana_inicio - timedelta(days=7)
        self.configuracao.data_fim_cronograma_ativo = self.semana_fim - timedelta(days=7)
        self.configuracao.save()

        versao_depois = self._consultar_versao(self.professor.id).json()['versao']

        self.assertNotEqual(versao_antes, versao_depois)

    def test_professor_id_inexistente_nao_quebra_e_usa_sentinela(self):
        response = self._consultar_versao(999999)

        self.assertEqual(response.status_code, 200)
        self.assertIn('versao', response.json())

    def test_professor_id_invalido_nao_quebra_e_usa_sentinela(self):
        response = self.client.get(
            reverse('versao_horario_professor_laboratorio_publico'),
            {'professor_id': 'abc'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('versao', response.json())

    def test_professor_inativo_usa_versao_sentinela_igual_a_invalido(self):
        professor_inativo = Professor.objects.create(nome_completo='Paulo Lima', ativo=False)

        versao_inativo = self._consultar_versao(professor_inativo.id).json()['versao']
        versao_inexistente = self._consultar_versao(999999).json()['versao']

        self.assertEqual(versao_inativo, versao_inexistente)

    def test_endpoint_e_somente_leitura_rejeita_post(self):
        response = self.client.post(
            reverse('versao_horario_professor_laboratorio_publico'),
            {'professor_id': str(self.professor.id)},
        )

        self.assertEqual(response.status_code, 405)

    def test_formato_json_contem_apenas_chave_versao(self):
        response = self._consultar_versao(self.professor.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json().keys()), {'versao'})

    def test_template_contem_js_de_polling_adaptativo(self):
        self._agendar(self.professor)
        response = self.client.get(
            reverse('consulta_publica_professor_laboratorio'),
            {'professor_id': str(self.professor.id)},
        )

        self.assertContains(response, 'versaoConhecidaHorarioPublico')
        self.assertContains(response, reverse('versao_horario_professor_laboratorio_publico'))

    def test_barra_de_aviso_inicia_oculta_no_html(self):
        self._agendar(self.professor)
        response = self.client.get(
            reverse('consulta_publica_professor_laboratorio'),
            {'professor_id': str(self.professor.id)},
        )

        self.assertContains(response, 'id="avisoAtualizacaoHorario"')
        self.assertNotContains(response, 'schedule-update-banner is-visible')


@override_settings(CONSULTA_LABORATORIO_EM_MANUTENCAO=False)
class ConsultaPublicaProfessorVersaoAtivaTests(TestCase):
    """A consulta publica deve mostrar somente horarios pertencentes a versao
    ATIVA (SemanaCronogramaLaboratorio) de cada turno - nunca editando,
    historica ou cancelada -, selecionando os AgendamentoLab pela FK
    'semana' da versao ativa. Nunca pela data atual, data_inicio + 6 dias,
    _inicio_semana(), agendamento mais recente ou campos legados de
    ConfiguracaoCronogramaLaboratorio."""

    def setUp(self):
        self.registrador = User.objects.create_user(username='registrador-versao-ativa', password='senha-teste')
        self.turma = Turma.objects.create(nome='2A', ano=2026, serie='2 Ano', ativa=True, turno='manha')
        self.laboratorio = Laboratorio.objects.create(nome='Laboratorio Versao', tipo='fixo', equipamento='Computadores', ativo=True)
        self.professor = Professor.objects.create(nome_completo='Marcia Nunes', ativo=True)
        self.segunda = date(2026, 5, 18)  # segunda-feira

    def _semana(self, turno, data_inicio, status):
        return SemanaCronogramaLaboratorio.objects.create(turno=turno, data_inicio=data_inicio, status=status)

    def _agendar(self, semana, turno, data, horario, disciplina):
        return AgendamentoLab.objects.create(
            laboratorio=self.laboratorio,
            data=data,
            horario=horario,
            turno=turno,
            professor=self.professor,
            turma=self.turma,
            disciplina=disciplina,
            registrado_por=self.registrador,
            semana=semana,
        )

    def _consultar(self):
        return self.client.get(
            reverse('consulta_publica_professor_laboratorio'),
            {'professor_id': str(self.professor.id)},
        )

    def _versao(self):
        return self.client.get(
            reverse('versao_horario_professor_laboratorio_publico'),
            {'professor_id': str(self.professor.id)},
        ).json()['versao']

    def test_mostra_horario_da_ativa_manha(self):
        semana_ativa = self._semana('manha', self.segunda, 'ativa')
        self._agendar(semana_ativa, 'manha', self.segunda, '1', 'Matematica Ativa Manha')

        response = self._consultar()

        self.assertContains(response, 'Matematica Ativa Manha')

    def test_mostra_horario_da_ativa_tarde(self):
        semana_ativa = self._semana('tarde', self.segunda, 'ativa')
        self._agendar(semana_ativa, 'tarde', self.segunda, '1', 'Fisica Ativa Tarde')

        response = self._consultar()

        self.assertContains(response, 'Fisica Ativa Tarde')

    def test_professor_presente_nos_dois_turnos(self):
        semana_manha = self._semana('manha', self.segunda, 'ativa')
        semana_tarde = self._semana('tarde', self.segunda, 'ativa')
        self._agendar(semana_manha, 'manha', self.segunda, '1', 'Matematica Manha')
        self._agendar(semana_tarde, 'tarde', self.segunda, '1', 'Fisica Tarde')

        response = self._consultar()

        self.assertContains(response, 'Matematica Manha')
        self.assertContains(response, 'Fisica Tarde')

    def test_editando_e_ativa_mostra_somente_ativa(self):
        semana_editando = self._semana('manha', self.segunda + timedelta(days=7), 'editando')
        semana_ativa = self._semana('manha', self.segunda, 'ativa')
        self._agendar(semana_editando, 'manha', self.segunda + timedelta(days=7), '1', 'Editando Nao Deve Aparecer')
        self._agendar(semana_ativa, 'manha', self.segunda, '2', 'Ativa Deve Aparecer')

        response = self._consultar()

        self.assertContains(response, 'Ativa Deve Aparecer')
        self.assertNotContains(response, 'Editando Nao Deve Aparecer')

    def test_historica_e_ativa_mostra_somente_ativa(self):
        semana_historica = self._semana('manha', self.segunda - timedelta(days=14), 'historica')
        semana_ativa = self._semana('manha', self.segunda, 'ativa')
        self._agendar(semana_historica, 'manha', self.segunda - timedelta(days=14), '1', 'Historica Nao Deve Aparecer')
        self._agendar(semana_ativa, 'manha', self.segunda, '1', 'Ativa Deve Aparecer')

        response = self._consultar()

        self.assertContains(response, 'Ativa Deve Aparecer')
        self.assertNotContains(response, 'Historica Nao Deve Aparecer')

    def test_cancelada_nao_aparece(self):
        semana_cancelada = self._semana('manha', self.segunda - timedelta(days=7), 'cancelada')
        semana_ativa = self._semana('manha', self.segunda, 'ativa')
        self._agendar(semana_cancelada, 'manha', self.segunda - timedelta(days=7), '1', 'Cancelada Nao Deve Aparecer')
        self._agendar(semana_ativa, 'manha', self.segunda, '1', 'Ativa Deve Aparecer')

        response = self._consultar()

        self.assertContains(response, 'Ativa Deve Aparecer')
        self.assertNotContains(response, 'Cancelada Nao Deve Aparecer')

    def test_varias_versoes_em_edicao_nao_interferem(self):
        semana_ativa = self._semana('manha', self.segunda, 'ativa')
        semana_editando_1 = self._semana('manha', self.segunda + timedelta(days=7), 'editando')
        semana_editando_2 = self._semana('manha', self.segunda + timedelta(days=14), 'editando')
        self._agendar(semana_ativa, 'manha', self.segunda, '1', 'Ativa Deve Aparecer')
        self._agendar(semana_editando_1, 'manha', self.segunda + timedelta(days=7), '1', 'Editando Um Nao Deve Aparecer')
        self._agendar(semana_editando_2, 'manha', self.segunda + timedelta(days=14), '1', 'Editando Dois Nao Deve Aparecer')

        response = self._consultar()

        self.assertContains(response, 'Ativa Deve Aparecer')
        self.assertNotContains(response, 'Editando Um Nao Deve Aparecer')
        self.assertNotContains(response, 'Editando Dois Nao Deve Aparecer')

    def test_versoes_com_datas_sobrepostas_nao_misturam_horarios(self):
        # Uma 'historica' pode compartilhar data_inicio com a 'ativa' atual
        # (o unique constraint do modelo so proibe isso entre
        # editando/ativa) - a selecao precisa usar a FK 'semana', nunca o
        # intervalo de datas, para nao misturar as duas versoes.
        semana_historica = self._semana('manha', self.segunda, 'historica')
        self._agendar(semana_historica, 'manha', self.segunda, '1', 'Historica Sobreposta Nao Deve Aparecer')

        semana_ativa = self._semana('manha', self.segunda, 'ativa')
        self._agendar(semana_ativa, 'manha', self.segunda, '2', 'Ativa Sobreposta Deve Aparecer')

        response = self._consultar()

        self.assertContains(response, 'Ativa Sobreposta Deve Aparecer')
        self.assertNotContains(response, 'Historica Sobreposta Nao Deve Aparecer')

    def test_troca_da_ativa_mostra_nova_versao(self):
        semana_antiga = self._semana('manha', self.segunda, 'ativa')
        self._agendar(semana_antiga, 'manha', self.segunda, '1', 'Versao Antiga')

        response_antes = self._consultar()
        self.assertContains(response_antes, 'Versao Antiga')

        semana_antiga.status = 'historica'
        semana_antiga.save()
        semana_nova = self._semana('manha', self.segunda + timedelta(days=7), 'ativa')
        self._agendar(semana_nova, 'manha', self.segunda + timedelta(days=7), '1', 'Versao Nova')

        response_depois = self._consultar()

        self.assertContains(response_depois, 'Versao Nova')
        self.assertNotContains(response_depois, 'Versao Antiga')

    def test_ativa_antiga_que_virou_historica_deixa_de_aparecer(self):
        semana = self._semana('manha', self.segunda, 'ativa')
        self._agendar(semana, 'manha', self.segunda, '1', 'Vira Historica')

        response_antes = self._consultar()
        self.assertContains(response_antes, 'Vira Historica')

        semana.status = 'historica'
        semana.save()

        response_depois = self._consultar()

        self.assertNotContains(response_depois, 'Vira Historica')

    def test_cronograma_ativo_antigo_continua_aparecendo_com_passagem_do_tempo(self):
        data_bem_antiga = date(2020, 1, 6)  # segunda-feira, anos antes de hoje
        semana_antiga = self._semana('manha', data_bem_antiga, 'ativa')
        self._agendar(semana_antiga, 'manha', data_bem_antiga, '1', 'Ativa Antiga Ainda Vale')

        response = self._consultar()

        self.assertContains(response, 'Ativa Antiga Ainda Vale')

    def test_manha_e_tarde_independentes(self):
        semana_manha = self._semana('manha', self.segunda, 'ativa')
        semana_tarde = self._semana('tarde', self.segunda + timedelta(days=21), 'ativa')
        self._agendar(semana_manha, 'manha', self.segunda, '1', 'Manha Semana A')
        self._agendar(semana_tarde, 'tarde', self.segunda + timedelta(days=21), '1', 'Tarde Semana B')

        response = self._consultar()

        self.assertContains(response, 'Manha Semana A')
        self.assertContains(response, 'Tarde Semana B')

    def test_consulta_continua_publica_sem_autenticacao(self):
        semana_ativa = self._semana('manha', self.segunda, 'ativa')
        self._agendar(semana_ativa, 'manha', self.segunda, '1', 'Acesso Publico')

        response = self._consultar()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Acesso Publico')

    def test_versao_muda_quando_ativa_e_trocada_mesmo_com_conteudo_identico(self):
        # Regressao: o fingerprint de polling precisa mudar quando a versao
        # ativa e trocada mesmo que a nova reaproveite a mesma data_inicio e
        # o mesmo conteudo da anterior - por isso a identidade usada no hash
        # inclui o id da linha de SemanaCronogramaLaboratorio, nao so datas.
        semana_antiga = self._semana('manha', self.segunda, 'ativa')
        agendamento_antigo = self._agendar(semana_antiga, 'manha', self.segunda, '1', 'Conteudo Igual')
        versao_antes = self._versao()

        semana_antiga.status = 'historica'
        semana_antiga.save()
        agendamento_antigo.delete()
        semana_nova = self._semana('manha', self.segunda, 'ativa')
        self._agendar(semana_nova, 'manha', self.segunda, '1', 'Conteudo Igual')

        versao_depois = self._versao()

        self.assertNotEqual(versao_antes, versao_depois)


class ChamadaV12Tests(TestCase):
    def setUp(self):
        digitadores = Group.objects.create(name='Digitadores')
        self.digitador = User.objects.create_user(username='digitador-v12', password='senha-teste')
        self.digitador.groups.add(digitadores)

        equipe = Group.objects.create(name='Equipe Diretiva')
        self.equipe_user = User.objects.create_user(username='equipe-v12', password='senha-teste')
        self.equipe_user.groups.add(equipe)

        professores = Group.objects.create(name='Professores')
        self.professor_sem_acesso = User.objects.create_user(username='professor-v12', password='senha-teste')
        self.professor_sem_acesso.groups.add(professores)

        self.turma_a = Turma.objects.create(nome='6A', ano=2026, serie='6 Ano', ativa=True, turno='manha')
        self.turma_b = Turma.objects.create(nome='6B', ano=2026, serie='6 Ano', ativa=True, turno='manha')
        self.turma_tarde = Turma.objects.create(nome='7A', ano=2026, serie='7 Ano', ativa=True, turno='tarde')

        self.aluno_1 = Aluno.objects.create(nome='Aluno Um', numero=1, turma=self.turma_a, ativo=True)
        self.aluno_2 = Aluno.objects.create(nome='Aluno Dois', numero=2, turma=self.turma_a, ativo=True)
        self.aluno_inativo = Aluno.objects.create(nome='Aluno Transferido', numero=3, turma=self.turma_a, ativo=False)
        self.aluno_atestado = Aluno.objects.create(nome='Aluno Atestado', numero=4, turma=self.turma_a, ativo=True)
        self.aluno_outra_turma = Aluno.objects.create(nome='Aluno Outra Turma', numero=1, turma=self.turma_b, ativo=True)

        self.data_chamada = date(2026, 8, 3)
        AtestadoAluno.objects.create(
            aluno=self.aluno_atestado,
            data_inicio=self.data_chamada - timedelta(days=2),
            data_fim=self.data_chamada + timedelta(days=2),
            ativo=True,
            criado_por=self.equipe_user,
        )

    def _url_registrar(self, turma=None, data=None):
        turma = turma or self.turma_a
        data = data or self.data_chamada
        return f"{reverse('chamada_v1_2_registrar')}?turma={turma.id}&data={data.isoformat()}"

    def _post_registrar(self, user, falta_ids, turma=None, data=None):
        turma = turma or self.turma_a
        data = data or self.data_chamada
        self.client.force_login(user)
        return self.client.post(reverse('chamada_v1_2_registrar'), {
            'turma': turma.id,
            'data': data.isoformat(),
            'falta_ids': falta_ids,
        })

    def _post_atestado(self, user, aluno, data_inicio, data_fim, turma=None, data_chamada=None):
        turma = turma or self.turma_a
        data_chamada = data_chamada or self.data_chamada
        self.client.force_login(user)
        return self.client.post(reverse('chamada_v1_2_atestado'), {
            'turma': turma.id,
            'aluno': aluno.id,
            'data': data_chamada.isoformat(),
            'data_inicio': data_inicio.isoformat(),
            'data_fim': data_fim.isoformat(),
        })

    def _aviso_atestado_oculto(self, conteudo, aluno_id):
        # O bloco "ATESTADO ATIVO" fica sempre no DOM (para o JS poder
        # reexibi-lo apos confirmar um atestado via AJAX sem recarregar a
        # pagina) - a visibilidade real e controlada pela classe 'd-none' no
        # <div> pai, entao o teste precisa olhar a classe, nao so o texto.
        marcador = f'id="aviso-atestado-{aluno_id}"'
        indice = conteudo.index(marcador)
        trecho_antes = conteudo[max(indice - 80, 0):indice]
        return 'd-none' in trecho_antes

    def test_acesso_permitido_para_digitador(self):
        self.client.force_login(self.digitador)

        response = self.client.get(reverse('chamada_v1_2_selecionar'))

        self.assertEqual(response.status_code, 200)

    def test_usuario_sem_permissao_bloqueado(self):
        self.client.force_login(self.professor_sem_acesso)

        response = self.client.get(reverse('chamada_v1_2_selecionar'))

        self.assertEqual(response.status_code, 403)

    def test_listagem_somente_alunos_da_turma(self):
        self.client.force_login(self.digitador)

        response = self.client.get(self._url_registrar())

        alunos_contexto = list(response.context['alunos'])
        self.assertIn(self.aluno_1, alunos_contexto)
        self.assertIn(self.aluno_2, alunos_contexto)

    def test_nao_mistura_alunos_de_turmas_diferentes(self):
        self.client.force_login(self.digitador)

        response = self.client.get(self._url_registrar())

        alunos_contexto = list(response.context['alunos'])
        self.assertNotIn(self.aluno_outra_turma, alunos_contexto)
        self.assertNotContains(response, 'Aluno Outra Turma')

    def test_registro_de_presentes_e_faltas(self):
        response = self._post_registrar(self.digitador, [str(self.aluno_2.id)])

        self.assertEqual(response.status_code, 200)
        dados = response.json()
        self.assertEqual(dados['status'], 'ok')
        self.assertTrue(RegistroFaltaAluno.objects.filter(aluno=self.aluno_2, data=self.data_chamada).exists())
        self.assertFalse(RegistroFaltaAluno.objects.filter(aluno=self.aluno_1, data=self.data_chamada).exists())

    def test_respeita_aluno_transferido_ou_inativo(self):
        self.client.force_login(self.digitador)
        response_grid = self.client.get(self._url_registrar())
        self.assertNotIn(self.aluno_inativo, list(response_grid.context['alunos']))

        response_post = self._post_registrar(self.digitador, [str(self.aluno_inativo.id)])

        self.assertEqual(response_post.status_code, 200)
        self.assertFalse(RegistroFaltaAluno.objects.filter(aluno=self.aluno_inativo).exists())

    def test_respeita_atestado_ativo(self):
        response = self._post_registrar(self.digitador, [str(self.aluno_atestado.id)])

        self.assertEqual(response.status_code, 200)
        self.assertFalse(RegistroFaltaAluno.objects.filter(aluno=self.aluno_atestado, data=self.data_chamada).exists())

    def test_impede_chamada_duplicada(self):
        RegistroFaltaAluno.objects.create(
            aluno=self.aluno_1,
            data=self.data_chamada,
            quantidade_faltas=1,
            registrado_por=self.digitador,
        )

        response = self._post_registrar(self.digitador, [str(self.aluno_2.id)])

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['status'], 'duplicado')
        self.assertFalse(RegistroFaltaAluno.objects.filter(aluno=self.aluno_2).exists())

    def test_reenvio_da_mesma_requisicao_nao_duplica_faltas(self):
        primeira = self._post_registrar(self.digitador, [str(self.aluno_2.id)])
        segunda = self._post_registrar(self.digitador, [str(self.aluno_2.id)])

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 409)
        self.assertEqual(
            RegistroFaltaAluno.objects.filter(aluno=self.aluno_2, data=self.data_chamada).count(),
            1,
        )

    def test_get_or_create_nao_cria_registro_duplicado_para_mesmo_aluno_e_data(self):
        # Testa diretamente o mecanismo que substituiu o create() simples:
        # get_or_create() com os mesmos (aluno, data) so grava uma vez.
        primeiro, criado_1 = RegistroFaltaAluno.objects.get_or_create(
            aluno=self.aluno_1,
            data=self.data_chamada,
            defaults={'quantidade_faltas': 1, 'registrado_por': self.digitador},
        )
        segundo, criado_2 = RegistroFaltaAluno.objects.get_or_create(
            aluno=self.aluno_1,
            data=self.data_chamada,
            defaults={'quantidade_faltas': 1, 'registrado_por': self.digitador},
        )

        self.assertTrue(criado_1)
        self.assertFalse(criado_2)
        self.assertEqual(primeiro.pk, segundo.pk)
        self.assertEqual(
            RegistroFaltaAluno.objects.filter(aluno=self.aluno_1, data=self.data_chamada).count(),
            1,
        )

    def test_corrida_logica_bypassando_checagem_de_duplicidade_nao_duplica_falta(self):
        # Simula uma corrida real: outra requisicao concorrente ja tinha
        # criado o registro do aluno_1 (e ja commitou) no exato instante em
        # que a checagem _chamada_ja_registrada desta requisicao rodou - por
        # isso ela e forcada (via mock) a nao enxergar a duplicidade, exatamente
        # como poderia acontecer numa corrida de verdade sem UniqueConstraint
        # no banco. A defesa que resta e a releitura de registros_existentes
        # e o get_or_create() dentro do mesmo lock/transacao.
        RegistroFaltaAluno.objects.create(
            aluno=self.aluno_1,
            data=self.data_chamada,
            quantidade_faltas=1,
            registrado_por=self.digitador,
        )

        with patch('apps.views.chamada_v1_2._chamada_ja_registrada', return_value=False):
            response = self._post_registrar(self.digitador, [str(self.aluno_1.id), str(self.aluno_2.id)])

        self.assertEqual(response.status_code, 200)
        dados = response.json()
        self.assertEqual(dados['status'], 'ok')
        self.assertEqual(
            RegistroFaltaAluno.objects.filter(aluno=self.aluno_1, data=self.data_chamada).count(),
            1,
        )
        self.assertTrue(RegistroFaltaAluno.objects.filter(aluno=self.aluno_2, data=self.data_chamada).exists())
        self.assertEqual(dados['faltosos'], 1)

    def test_conferir_salvar_bloqueia_turma_e_nao_duplica_falta_existente(self):
        RegistroFaltaAluno.objects.create(
            aluno=self.aluno_1,
            data=self.data_chamada,
            quantidade_faltas=1,
            registrado_por=self.digitador,
        )
        self.client.force_login(self.equipe_user)

        response = self.client.post(reverse('chamada_v1_2_conferir_salvar'), {
            'turma': self.turma_a.id,
            'data': self.data_chamada.isoformat(),
            'falta_ids': [str(self.aluno_1.id)],
        })

        self.assertEqual(response.status_code, 200)
        dados = response.json()
        self.assertEqual(dados['status'], 'ok')
        self.assertEqual(dados['adicionadas'], 0)
        self.assertEqual(
            RegistroFaltaAluno.objects.filter(aluno=self.aluno_1, data=self.data_chamada).count(),
            1,
        )

    def test_edicao_de_chamada_existente(self):
        RegistroFaltaAluno.objects.create(
            aluno=self.aluno_1,
            data=self.data_chamada,
            quantidade_faltas=1,
            registrado_por=self.digitador,
        )
        self.client.force_login(self.equipe_user)

        response = self.client.post(reverse('chamada_v1_2_conferir_salvar'), {
            'turma': self.turma_a.id,
            'data': self.data_chamada.isoformat(),
            'falta_ids': [str(self.aluno_2.id)],
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')
        self.assertFalse(RegistroFaltaAluno.objects.filter(aluno=self.aluno_1, data=self.data_chamada).exists())
        self.assertTrue(RegistroFaltaAluno.objects.filter(aluno=self.aluno_2, data=self.data_chamada).exists())

    def test_usuario_nao_autorizado_nao_consegue_editar(self):
        self.client.force_login(self.digitador)

        response = self.client.post(reverse('chamada_v1_2_conferir_salvar'), {
            'turma': self.turma_a.id,
            'data': self.data_chamada.isoformat(),
            'falta_ids': [str(self.aluno_1.id)],
        })

        self.assertEqual(response.status_code, 302)
        self.assertFalse(RegistroFaltaAluno.objects.filter(aluno=self.aluno_1).exists())

    def test_data_exibida_corretamente(self):
        self.client.force_login(self.digitador)

        response = self.client.get(self._url_registrar())

        self.assertEqual(response.context['data'], self.data_chamada)
        self.assertContains(response, '03/08/2026')

    def test_tela_de_registro_mostra_apenas_a_data_atual(self):
        RegistroFaltaAluno.objects.create(
            aluno=self.aluno_1,
            data=date(2026, 5, 10),
            quantidade_faltas=1,
            registrado_por=self.digitador,
        )
        self.client.force_login(self.digitador)

        response = self.client.get(self._url_registrar(data=date(2026, 8, 4)))

        self.assertNotContains(response, '10/05/2026')
        self.assertEqual(response.context['data'], date(2026, 8, 4))

    def test_chamada_atual_permanece_intacta(self):
        self.client.force_login(self.digitador)

        response = self.client.get(reverse('registrar_falta_aluno'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Registrar Falta de Aluno')

    def test_transacao_revertida_em_caso_de_erro(self):
        self.client.raise_request_exception = False

        with patch(
            'apps.views.chamada_v1_2.RegistroFaltaAluno.objects.get_or_create',
            side_effect=RuntimeError('Falha simulada'),
        ):
            response = self._post_registrar(self.digitador, [str(self.aluno_1.id), str(self.aluno_2.id)])

        self.assertEqual(response.status_code, 500)
        self.assertFalse(RegistroFaltaAluno.objects.filter(data=self.data_chamada).exists())

    # ------------------------------------------------------------------
    # Turmas separadas por turno na tela inicial
    # ------------------------------------------------------------------

    def test_selecionar_agrupa_turmas_por_turno(self):
        self.client.force_login(self.digitador)

        response = self.client.get(reverse('chamada_v1_2_selecionar'))

        self.assertIn(self.turma_a, list(response.context['turmas_manha']))
        self.assertIn(self.turma_b, list(response.context['turmas_manha']))
        self.assertNotIn(self.turma_tarde, list(response.context['turmas_manha']))
        self.assertIn(self.turma_tarde, list(response.context['turmas_tarde']))
        self.assertNotIn(self.turma_a, list(response.context['turmas_tarde']))
        self.assertContains(response, 'TURNO DA MANHÃ')
        self.assertContains(response, 'TURNO DA TARDE')

    def test_selecionar_somente_turmas_ativas(self):
        turma_inativa = Turma.objects.create(nome='9Z', ano=2026, serie='9 Ano', ativa=False, turno='manha')
        self.client.force_login(self.digitador)

        response = self.client.get(reverse('chamada_v1_2_selecionar'))

        self.assertNotIn(turma_inativa, list(response.context['turmas_manha']))
        self.assertNotContains(response, '9Z')

    def test_selecionar_turno_vazio_nao_exibido(self):
        self.turma_tarde.ativa = False
        self.turma_tarde.save(update_fields=['ativa'])
        self.client.force_login(self.digitador)

        response = self.client.get(reverse('chamada_v1_2_selecionar'))

        self.assertNotContains(response, 'TURNO DA TARDE')
        self.assertContains(response, 'TURNO DA MANHÃ')

    # ------------------------------------------------------------------
    # Atestado direto na Chamada v1.2
    # ------------------------------------------------------------------

    def test_cadastro_de_atestado_com_periodo_valido(self):
        response = self._post_atestado(
            self.equipe_user,
            self.aluno_1,
            self.data_chamada,
            self.data_chamada + timedelta(days=3),
        )

        self.assertEqual(response.status_code, 200)
        dados = response.json()
        self.assertEqual(dados['status'], 'ok')
        self.assertTrue(dados['criado'])
        atestado = AtestadoAluno.objects.get(aluno=self.aluno_1)
        self.assertTrue(atestado.ativo)
        self.assertEqual(atestado.data_inicio, self.data_chamada)
        self.assertEqual(atestado.data_fim, self.data_chamada + timedelta(days=3))

    def test_atestado_rejeita_data_final_anterior_a_inicial(self):
        response = self._post_atestado(
            self.equipe_user,
            self.aluno_1,
            self.data_chamada,
            self.data_chamada - timedelta(days=1),
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AtestadoAluno.objects.filter(aluno=self.aluno_1).exists())

    def test_aluno_com_atestado_cadastrado_pela_chamada_nao_recebe_falta(self):
        resposta_atestado = self._post_atestado(
            self.equipe_user,
            self.aluno_1,
            self.data_chamada,
            self.data_chamada + timedelta(days=2),
        )
        self.assertEqual(resposta_atestado.status_code, 200)

        response = self._post_registrar(self.digitador, [str(self.aluno_1.id), str(self.aluno_2.id)])

        self.assertEqual(response.status_code, 200)
        self.assertFalse(RegistroFaltaAluno.objects.filter(aluno=self.aluno_1, data=self.data_chamada).exists())
        self.assertTrue(RegistroFaltaAluno.objects.filter(aluno=self.aluno_2, data=self.data_chamada).exists())

    def test_atestado_cadastrado_remove_falta_ja_lancada_na_chamada(self):
        RegistroFaltaAluno.objects.create(
            aluno=self.aluno_1,
            data=self.data_chamada,
            quantidade_faltas=1,
            registrado_por=self.digitador,
        )

        response = self._post_atestado(
            self.equipe_user,
            self.aluno_1,
            self.data_chamada,
            self.data_chamada + timedelta(days=1),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(RegistroFaltaAluno.objects.filter(aluno=self.aluno_1, data=self.data_chamada).exists())

    def test_atestado_existente_e_exibido_na_grade(self):
        self.client.force_login(self.digitador)

        response = self.client.get(self._url_registrar())

        atestado = self.aluno_atestado.atestados.first()
        self.assertContains(response, 'ATESTADO ATIVO')
        self.assertContains(response, f'De {atestado.data_inicio:%d/%m/%Y} até {atestado.data_fim:%d/%m/%Y}')

    def test_aviso_nao_exibido_quando_data_fora_do_periodo_do_atestado(self):
        self.client.force_login(self.digitador)
        atestado = self.aluno_atestado.atestados.first()
        data_fora_do_periodo = atestado.data_fim + timedelta(days=10)

        response = self.client.get(self._url_registrar(data=data_fora_do_periodo))
        conteudo = response.content.decode()

        self.assertTrue(self._aviso_atestado_oculto(conteudo, self.aluno_atestado.id))
        self.assertNotIn(self.aluno_atestado.id, response.context['atestado_ids'])

    def test_atestado_inativo_nao_bloqueia_falta(self):
        aluno_atestado_inativo = Aluno.objects.create(nome='Aluno Atestado Inativo', numero=5, turma=self.turma_a, ativo=True)
        AtestadoAluno.objects.create(
            aluno=aluno_atestado_inativo,
            data_inicio=self.data_chamada - timedelta(days=1),
            data_fim=self.data_chamada + timedelta(days=1),
            ativo=False,
            criado_por=self.equipe_user,
        )
        self.client.force_login(self.digitador)

        response = self.client.get(self._url_registrar())
        conteudo = response.content.decode()

        self.assertTrue(self._aviso_atestado_oculto(conteudo, aluno_atestado_inativo.id))
        self.assertNotIn(aluno_atestado_inativo.id, response.context['atestado_ids'])

        response_post = self._post_registrar(self.digitador, [str(aluno_atestado_inativo.id)])
        self.assertTrue(
            RegistroFaltaAluno.objects.filter(aluno=aluno_atestado_inativo, data=self.data_chamada).exists()
        )

    def test_botoes_presenca_falta_ficam_desabilitados_com_atestado_ativo(self):
        self.client.force_login(self.digitador)

        response = self.client.get(self._url_registrar())
        conteudo = response.content.decode()

        self.assertIn(f'id="presente_{self.aluno_atestado.id}"', conteudo)
        self.assertIn(f'id="falta_{self.aluno_atestado.id}"', conteudo)
        # Os dois radios do aluno com atestado ativo devem trazer o atributo disabled.
        inicio_bloco = conteudo.index(f'data-aluno-id="{self.aluno_atestado.id}"')
        bloco_aluno = conteudo[inicio_bloco:inicio_bloco + 1200]
        self.assertEqual(bloco_aluno.count('disabled'), 2)

    def test_html_responsivo_nao_remove_botoes_da_grade(self):
        # O teste automatizado nao renderiza CSS/viewport real (isso exige
        # verificacao manual no navegador em telas pequenas), mas garante que
        # a estrutura responsiva (container flex proprio + os tres botoes)
        # continua presente no HTML gerado pelo servidor.
        self.client.force_login(self.equipe_user)

        response = self.client.get(self._url_registrar())

        self.assertContains(response, 'chamada-botoes-status')
        self.assertContains(response, f'id="presente_{self.aluno_1.id}"')
        self.assertContains(response, f'id="falta_{self.aluno_1.id}"')
        self.assertContains(response, 'btn-abrir-atestado')

    def test_tentativa_de_duplicar_atestado_reaproveita_existente(self):
        primeira = self._post_atestado(
            self.equipe_user,
            self.aluno_1,
            self.data_chamada,
            self.data_chamada + timedelta(days=2),
        )
        segunda = self._post_atestado(
            self.equipe_user,
            self.aluno_1,
            self.data_chamada,
            self.data_chamada + timedelta(days=2),
        )

        self.assertTrue(primeira.json()['criado'])
        self.assertFalse(segunda.json()['criado'])
        self.assertEqual(AtestadoAluno.objects.filter(aluno=self.aluno_1).count(), 1)

    def test_reenvio_da_mesma_requisicao_nao_duplica_atestado(self):
        respostas = [
            self._post_atestado(
                self.equipe_user,
                self.aluno_1,
                self.data_chamada,
                self.data_chamada + timedelta(days=1),
            )
            for _ in range(2)
        ]

        for resposta in respostas:
            self.assertEqual(resposta.status_code, 200)
        self.assertEqual(AtestadoAluno.objects.filter(aluno=self.aluno_1).count(), 1)

    def test_atestado_usuario_sem_permissao_bloqueado(self):
        response = self._post_atestado(
            self.professor_sem_acesso,
            self.aluno_1,
            self.data_chamada,
            self.data_chamada + timedelta(days=1),
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(AtestadoAluno.objects.filter(aluno=self.aluno_1).exists())

    def test_atestado_digitador_bloqueado(self):
        # Digitador pode lancar/ver a chamada v1.2 normalmente, mas o
        # endpoint de atestado agora e restrito a superuser/Equipe Diretiva
        # (justificar_faltas_required), igual ao CRUD antigo de atestados.
        response = self._post_atestado(
            self.digitador,
            self.aluno_1,
            self.data_chamada,
            self.data_chamada + timedelta(days=1),
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(AtestadoAluno.objects.filter(aluno=self.aluno_1).exists())

    def test_digitador_continua_registrando_presente_falta_normalmente(self):
        response = self._post_registrar(self.digitador, [str(self.aluno_2.id)])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')
        self.assertTrue(RegistroFaltaAluno.objects.filter(aluno=self.aluno_2, data=self.data_chamada).exists())

    def test_botao_atestado_aparece_so_para_quem_pode_justificar_faltas(self):
        # A combinacao dessas classes so aparece no <button> real; as duas
        # ocorrencias em JS (querySelectorAll/querySelector) usam apenas o
        # seletor '.btn-abrir-atestado' com ponto, entao esse trecho maior
        # nao gera falso positivo vindo do <script>.
        marcador_botao = 'btn-outline-info chamada-botao-status btn-abrir-atestado'

        self.client.force_login(self.equipe_user)
        response_equipe = self.client.get(self._url_registrar())
        self.assertContains(response_equipe, marcador_botao)

        self.client.force_login(self.digitador)
        response_digitador = self.client.get(self._url_registrar())
        self.assertNotContains(response_digitador, marcador_botao)

    def test_botao_atestado_fica_oculto_quando_ja_ha_atestado_ativo(self):
        self.client.force_login(self.equipe_user)

        response = self.client.get(self._url_registrar())
        conteudo = response.content.decode()

        # Na turma_a, so aluno_atestado (criado no setUp) tem atestado ativo
        # cobrindo self.data_chamada - entao exatamente um botao ATESTADO
        # deve sair renderizado com a classe 'd-none' (oculto, mas ainda no
        # DOM para o JS poder reexibi-lo se o atestado expirar).
        self.assertEqual(conteudo.count('btn-abrir-atestado d-none'), 1)

    def test_atestado_de_aluno_de_outra_turma_rejeitado(self):
        response = self._post_atestado(
            self.equipe_user,
            self.aluno_outra_turma,
            self.data_chamada,
            self.data_chamada + timedelta(days=1),
            turma=self.turma_a,
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AtestadoAluno.objects.filter(aluno=self.aluno_outra_turma).exists())

    def test_atestado_e_chamada_tratados_em_transacao(self):
        self.client.raise_request_exception = False
        RegistroFaltaAluno.objects.create(
            aluno=self.aluno_1,
            data=self.data_chamada,
            quantidade_faltas=1,
            registrado_por=self.digitador,
        )

        with patch(
            'apps.views.chamada_v1_2.AtestadoAluno.objects.create',
            side_effect=RuntimeError('Falha simulada'),
        ):
            response = self._post_atestado(
                self.equipe_user,
                self.aluno_1,
                self.data_chamada,
                self.data_chamada + timedelta(days=1),
            )

        self.assertEqual(response.status_code, 500)
        self.assertFalse(AtestadoAluno.objects.filter(aluno=self.aluno_1).exists())
        # A falta pre-existente nao pode ter sido removida: o erro no atestado
        # reverteu a transacao inteira (atestado + reconciliacao da chamada).
        self.assertTrue(RegistroFaltaAluno.objects.filter(aluno=self.aluno_1, data=self.data_chamada).exists())

    def test_conferir_respeita_atestado(self):
        self.client.force_login(self.equipe_user)
        atestado = self.aluno_atestado.atestados.first()

        response = self.client.get(
            f"{reverse('chamada_v1_2_conferir')}?turma={self.turma_a.id}&data={self.data_chamada.isoformat()}"
        )

        self.assertContains(response, 'ATESTADO ATIVO')
        self.assertContains(response, f'De {atestado.data_inicio:%d/%m/%Y} até {atestado.data_fim:%d/%m/%Y}')
        self.assertIn(self.aluno_atestado.id, response.context['atestado_ids'])

    def test_chamada_antiga_permanece_funcionando_apos_ajustes_de_atestado(self):
        self.client.force_login(self.digitador)

        response = self.client.get(reverse('registrar_falta_aluno'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Registrar Falta de Aluno')


class RelatorioSemanalFaltasCriterioDiasTests(TestCase):
    """
    Cobre a correcao do criterio de envio do relatorio semanal de faltas:
    o envio deve considerar a quantidade de DIAS distintos com falta (>= 2)
    e nao a soma de `quantidade_faltas`, que representa aulas/periodos.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='digitador-relatorio', password='senha-teste')
        self.turma = Turma.objects.create(nome='2A', ano=2026, serie='2 Ano', ativa=True, turno='manha')
        self.aluno = Aluno.objects.create(
            nome='Aluno Relatorio',
            numero=1,
            turma=self.turma,
            telefone='44999998888',
        )
        # Semana de segunda (03/08/2026) a sexta (07/08/2026).
        self.semana_inicio = date(2026, 8, 3)
        self.semana_fim = date(2026, 8, 7)
        self.command = RelatorioSemanalWhatsappCommand()

    def _criar_falta(self, data, quantidade_faltas=1, aluno=None):
        return RegistroFaltaAluno.objects.create(
            aluno=aluno or self.aluno,
            data=data,
            quantidade_faltas=quantidade_faltas,
            pedagoga='Sonia',
            registrado_por=self.user,
        )

    def _criar_atestado(self, data_inicio, data_fim, aluno=None):
        return AtestadoAluno.objects.create(
            aluno=aluno or self.aluno,
            data_inicio=data_inicio,
            data_fim=data_fim,
            ativo=True,
        )

    def test_um_dia_uma_falta_nao_atinge_dois_dias(self):
        self._criar_falta(date(2026, 8, 3), quantidade_faltas=1)

        resultado = self.command._faltas_por_aluno(self.semana_inicio, self.semana_fim)

        info = resultado[self.aluno.id]
        self.assertEqual(info['total'], 1)
        self.assertEqual(info['dias'], 1)
        self.assertFalse(info['dias'] >= 2)

    def test_um_dia_seis_aulas_nao_atinge_dois_dias(self):
        # Caso do bug original: 1 dia com 6 aulas faltadas nao deve mais
        # disparar o relatorio (antes, total=6 >= 2 fazia o envio ocorrer).
        self._criar_falta(date(2026, 8, 3), quantidade_faltas=6)

        resultado = self.command._faltas_por_aluno(self.semana_inicio, self.semana_fim)

        info = resultado[self.aluno.id]
        self.assertEqual(info['total'], 6)
        self.assertEqual(info['dias'], 1)
        self.assertFalse(info['dias'] >= 2)

    def test_dois_dias_uma_falta_cada_atinge_criterio(self):
        self._criar_falta(date(2026, 8, 3), quantidade_faltas=1)
        self._criar_falta(date(2026, 8, 4), quantidade_faltas=1)

        resultado = self.command._faltas_por_aluno(self.semana_inicio, self.semana_fim)

        info = resultado[self.aluno.id]
        self.assertEqual(info['total'], 2)
        self.assertEqual(info['dias'], 2)
        self.assertTrue(info['dias'] >= 2)

    def test_dois_dias_seis_aulas_cada_soma_doze(self):
        self._criar_falta(date(2026, 8, 3), quantidade_faltas=6)
        self._criar_falta(date(2026, 8, 4), quantidade_faltas=6)

        resultado = self.command._faltas_por_aluno(self.semana_inicio, self.semana_fim)

        info = resultado[self.aluno.id]
        self.assertEqual(info['total'], 12)
        self.assertEqual(info['dias'], 2)
        self.assertTrue(info['dias'] >= 2)

    def test_duas_entradas_mesmo_dia_contam_como_um_dia(self):
        self._criar_falta(date(2026, 8, 3), quantidade_faltas=1)
        self._criar_falta(date(2026, 8, 3), quantidade_faltas=2)

        resultado = self.command._faltas_por_aluno(self.semana_inicio, self.semana_fim)

        info = resultado[self.aluno.id]
        self.assertEqual(info['total'], 3)
        self.assertEqual(info['dias'], 1)
        self.assertFalse(info['dias'] >= 2)

    def test_falta_coberta_por_atestado_nao_conta_no_total_nem_nos_dias(self):
        self._criar_atestado(date(2026, 8, 3), date(2026, 8, 3))
        self._criar_falta(date(2026, 8, 3), quantidade_faltas=6)

        resultado = self.command._faltas_por_aluno(self.semana_inicio, self.semana_fim)

        self.assertNotIn(self.aluno.id, resultado)

    def test_um_dia_coberto_outro_descoberto_nao_atinge_criterio(self):
        self._criar_atestado(date(2026, 8, 3), date(2026, 8, 3))
        self._criar_falta(date(2026, 8, 3), quantidade_faltas=6)  # coberto por atestado
        self._criar_falta(date(2026, 8, 4), quantidade_faltas=1)  # descoberto

        resultado = self.command._faltas_por_aluno(self.semana_inicio, self.semana_fim)

        info = resultado[self.aluno.id]
        self.assertEqual(info['total'], 1)
        self.assertEqual(info['dias'], 1)
        self.assertFalse(info['dias'] >= 2)

    def test_dois_dias_descobertos_um_coberto_usa_apenas_descobertos(self):
        self._criar_atestado(date(2026, 8, 3), date(2026, 8, 3))
        self._criar_falta(date(2026, 8, 3), quantidade_faltas=6)  # coberto, ignorado
        self._criar_falta(date(2026, 8, 4), quantidade_faltas=1)  # descoberto
        self._criar_falta(date(2026, 8, 5), quantidade_faltas=1)  # descoberto

        resultado = self.command._faltas_por_aluno(self.semana_inicio, self.semana_fim)

        info = resultado[self.aluno.id]
        self.assertEqual(info['total'], 2)
        self.assertEqual(info['dias'], 2)
        self.assertTrue(info['dias'] >= 2)

    def test_ocorrencias_continuam_usando_total_de_registros(self):
        # Criterio de ocorrencias nao deve ser alterado por esta correcao:
        # continua sendo baseado no total de registros (>= 2), nao em dias.
        RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 8, 3),
            tipo_ocorrencia='atraso',
            faltou=False,
            turno='manha',
            atendido_por='Sonia',
            registrado_por=self.user,
        )
        RegistroOcorrenciaAluno.objects.create(
            aluno=self.aluno,
            data=date(2026, 8, 3),
            tipo_ocorrencia='uniforme',
            faltou=False,
            turno='manha',
            atendido_por='Sonia',
            registrado_por=self.user,
        )

        resultado = self.command._ocorrencias_por_aluno(self.semana_inicio, self.semana_fim)

        info = resultado[self.aluno.id]
        self.assertEqual(info['total'], 2)
        self.assertTrue(info['total'] >= 2)


class RelatorioSemanalWhatsappComandoTests(TestCase):
    """
    Testes de integracao do comando `enviar_relatorio_semanal_whatsapp`
    cobrindo o novo criterio (dias distintos) em modo --dry-run.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='digitador-comando', password='senha-teste')
        self.turma = Turma.objects.create(nome='2B', ano=2026, serie='2 Ano', ativa=True, turno='manha')
        self.aluno_um_dia = Aluno.objects.create(
            nome='Aluno Um Dia', numero=1, turma=self.turma, telefone='44988887777',
        )
        self.aluno_dois_dias = Aluno.objects.create(
            nome='Aluno Dois Dias', numero=2, turma=self.turma, telefone='44988886666',
        )

    def _criar_falta(self, aluno, data, quantidade_faltas=1):
        return RegistroFaltaAluno.objects.create(
            aluno=aluno,
            data=data,
            quantidade_faltas=quantidade_faltas,
            pedagoga='Sonia',
            registrado_por=self.user,
        )

    def test_dry_run_respeita_novo_criterio_de_dias(self):
        # 1 dia com 6 aulas faltadas: nao deve ser listado para envio.
        self._criar_falta(self.aluno_um_dia, date(2026, 8, 3), quantidade_faltas=6)
        # 2 dias distintos com 1 falta cada: deve ser listado para envio.
        self._criar_falta(self.aluno_dois_dias, date(2026, 8, 3), quantidade_faltas=1)
        self._criar_falta(self.aluno_dois_dias, date(2026, 8, 4), quantidade_faltas=1)

        out = StringIO()
        call_command(
            'enviar_relatorio_semanal_whatsapp',
            '--data', '2026-08-07',
            '--dry-run',
            stdout=out,
        )
        saida = out.getvalue()

        self.assertNotIn('Aluno Um Dia', saida)
        self.assertIn('Aluno Dois Dias', saida)
        self.assertIn('2 falta(s)', saida)
        self.assertIn('Relatorios simulados: 1.', saida)
        self.assertIn('Relatorios ignorados: 0.', saida)

    def test_dry_run_nao_altera_enviados_json(self):
        self._criar_falta(self.aluno_dois_dias, date(2026, 8, 3), quantidade_faltas=1)
        self._criar_falta(self.aluno_dois_dias, date(2026, 8, 4), quantidade_faltas=1)

        with tempfile.TemporaryDirectory() as tmpdir:
            arquivo_controle = Path(tmpdir) / 'enviados.json'
            arquivo_lock = Path(tmpdir) / 'enviando.lock'
            with patch(
                'apps.management.commands.enviar_relatorio_semanal_whatsapp.ARQUIVO_CONTROLE',
                arquivo_controle,
            ), patch(
                'apps.management.commands.enviar_relatorio_semanal_whatsapp.ARQUIVO_LOCK',
                arquivo_lock,
            ):
                out = StringIO()
                call_command(
                    'enviar_relatorio_semanal_whatsapp',
                    '--data', '2026-08-07',
                    '--dry-run',
                    stdout=out,
                )

                self.assertFalse(arquivo_controle.exists())
                self.assertFalse(arquivo_lock.exists())

    def test_correcao_nao_exige_criar_migration(self):
        out = StringIO()
        call_command('makemigrations', 'apps', '--check', '--dry-run', stdout=out, stderr=out)
        self.assertIn('No changes detected', out.getvalue())


# =============================================================================
# VERIFICACAO DO CRONOGRAMA DOS LABORATORIOS PELO PDF DE HORARIO DOS PROFESSORES
# =============================================================================

FIXTURE_PDF_HORARIO_MANHA = Path(__file__).resolve().parent / 'tests_fixtures' / 'horario_teste_manha.pdf'


class HorarioPdfParserTests(TestCase):
    """Parser puro: nao acessa banco, so le o PDF fixture."""

    def test_le_fixture_e_encontra_slots_esperados(self):
        with open(FIXTURE_PDF_HORARIO_MANHA, 'rb') as arquivo:
            turno, slots, avisos = ler_pdf_horario_professores(arquivo)

        self.assertEqual(turno, 'manha')
        self.assertEqual(avisos, [])
        self.assertEqual(len(slots), 60)  # 2 professores x 6 horarios x 5 dias

        aulas = [s for s in slots if s['situacao_pdf'] == 'aula']
        self.assertEqual(len(aulas), 1)
        self.assertEqual(aulas[0]['professor_pdf_texto'], 'PROFESSOR TESTE')
        self.assertEqual(aulas[0]['dia_semana'], 'Segunda-feira')
        self.assertEqual(aulas[0]['horario'], '3')
        self.assertEqual(aulas[0]['turma_pdf_texto'], '3B SIST')
        self.assertEqual(aulas[0]['disciplina_pdf_texto'], 'MATEM.')

        has = [s for s in slots if s['situacao_pdf'] == 'ha']
        self.assertEqual(len(has), 1)

        aguardando = [s for s in slots if s['situacao_pdf'] == 'aguardando_professor']
        self.assertEqual(len(aguardando), 1)
        self.assertEqual(aguardando[0]['professor_pdf_texto'], '')
        self.assertEqual(aguardando[0]['turma_pdf_texto'], '2A HUM')
        self.assertEqual(aguardando[0]['disciplina_pdf_texto'], 'INGLES')

    def test_upload_e_leitura_nao_alteram_nenhum_agendamento(self):
        Laboratorio.objects.create(nome='Lab. 01', tipo='fixo', equipamento='Computadores', ativo=True)
        professor = Professor.objects.create(nome_completo='PROFESSOR TESTE DA SILVA', ativo=True)
        turma = Turma.objects.create(nome='3B SIST', ano=2026, serie='3 Ano', ativa=True, turno='manha')
        AgendamentoLab.objects.create(
            laboratorio=Laboratorio.objects.first(),
            data=date(2026, 6, 1),
            horario='3',
            turno='manha',
            professor=professor,
            turma=turma,
            disciplina='MATEM.',
        )
        antes = list(AgendamentoLab.objects.values('id', 'turma_id', 'data', 'horario', 'turno'))

        with open(FIXTURE_PDF_HORARIO_MANHA, 'rb') as arquivo:
            ler_pdf_horario_professores(arquivo)

        depois = list(AgendamentoLab.objects.values('id', 'turma_id', 'data', 'horario', 'turno'))
        self.assertEqual(antes, depois)


class HorarioPdfMatchingTests(TestCase):
    def test_normalizar_trata_indicador_ordinal_como_espaco(self):
        # Regressao: 'º' se decompoe para 'o' via NFKD e colava no numero
        # (ex.: "1º C" virava "1o C"), quebrando a extracao de serie+letra.
        self.assertEqual(normalizar('1º C TÉC. Agro'), '1 C TEC AGRO')

    def test_extrair_chave_turma_reconhece_variacoes_do_pdf_real(self):
        self.assertEqual(extrair_chave_turma('3°C TEC Des. Sistemas'), ('3', 'C', 'SIST'))
        self.assertEqual(extrair_chave_turma('1º C TÉC. Agro'), ('1', 'C', 'AGRO'))
        self.assertEqual(extrair_chave_turma('2°A NEM (HUMANAS)'), ('2', 'A', 'HUM'))
        self.assertEqual(extrair_chave_turma('9°A'), ('9', 'A', ''))


class HorarioPdfComparacaoTests(TestCase):
    """Testa a comparacao pura (sem HTTP) entre slots do PDF e o cronograma ativo."""

    def setUp(self):
        self.laboratorio = Laboratorio.objects.create(nome='Lab. 01', tipo='fixo', equipamento='Computadores', ativo=True)
        self.professor = Professor.objects.create(nome_completo='PROFESSOR TESTE DA SILVA', ativo=True)
        self.turma_a = Turma.objects.create(nome='3B SIST', ano=2026, serie='3 Ano', ativa=True, turno='manha')
        self.turma_b = Turma.objects.create(nome='1A HUM', ano=2026, serie='1 Ano', ativa=True, turno='manha')

        segunda = date(2026, 6, 1)
        self.semana_inicio = segunda - timedelta(days=segunda.weekday())
        self.semana_fim = self.semana_inicio + timedelta(days=6)

        ProfessorPdfAlias.objects.create(texto_pdf='PROFESSOR TESTE', professor=self.professor)

        self.upload = HorarioProfessorPDF.objects.create(
            arquivo='horarios_pdf/teste.pdf', nome_original='teste.pdf', turno_detectado='manha',
        )

    def _slot(self, dia='Segunda-feira', horario='3', situacao='aula', turma_pdf='3B SIST', disciplina='MATEM.',
              professor_texto='PROFESSOR TESTE', heading='PROFESSOR TESTE'):
        return {
            'heading_pdf_texto': heading,
            'professor_pdf_texto': professor_texto,
            'dia_semana': dia,
            'horario': horario,
            'turma_pdf_texto': turma_pdf if situacao in ('aula', 'aguardando_professor') else '',
            'disciplina_pdf_texto': disciplina if situacao in ('aula', 'aguardando_professor') else '',
            'situacao_pdf': situacao,
        }

    def _agendar(self, turma, turno='manha', dia_offset=0, horario='3'):
        return AgendamentoLab.objects.create(
            laboratorio=self.laboratorio,
            data=self.semana_inicio + timedelta(days=dia_offset),
            horario=horario,
            turno=turno,
            professor=self.professor,
            turma=turma,
            disciplina='MATEM.',
        )

    def test_turma_igual_gera_confere(self):
        TurmaPdfAlias.objects.create(texto_pdf='3B SIST', turma=self.turma_a)
        agendamento = self._agendar(self.turma_a)

        divergencias = gerar_divergencias(self.upload, [self._slot()], self.semana_inicio, self.semana_fim)

        self.assertEqual(len(divergencias), 1)
        self.assertEqual(divergencias[0].status, 'confere')
        self.assertEqual(divergencias[0].agendamento_lab_id, agendamento.id)

    def test_turma_diferente_gera_conflito(self):
        TurmaPdfAlias.objects.create(texto_pdf='3B SIST', turma=self.turma_a)
        agendamento = self._agendar(self.turma_b)  # reserva do lab esta com outra turma

        divergencias = gerar_divergencias(self.upload, [self._slot()], self.semana_inicio, self.semana_fim)

        self.assertEqual(len(divergencias), 1)
        self.assertEqual(divergencias[0].status, 'conflito')
        self.assertEqual(divergencias[0].agendamento_lab_id, agendamento.id)

    def test_turma_nao_identificada_gera_verificar(self):
        # Nenhum TurmaPdfAlias resolvido para '3B SIST' (fica pendente).
        agendamento = self._agendar(self.turma_a)

        divergencias = gerar_divergencias(self.upload, [self._slot()], self.semana_inicio, self.semana_fim)

        self.assertEqual(len(divergencias), 1)
        self.assertEqual(divergencias[0].status, 'verificar')
        self.assertEqual(divergencias[0].agendamento_lab_id, agendamento.id)

    def test_hora_atividade_no_slot_do_professor_com_reserva_gera_verificar_nunca_conflito(self):
        agendamento = self._agendar(self.turma_a)
        slot_ha = self._slot(situacao='ha')

        divergencias = gerar_divergencias(self.upload, [slot_ha], self.semana_inicio, self.semana_fim)

        self.assertEqual(len(divergencias), 1)
        self.assertEqual(divergencias[0].status, 'verificar')
        self.assertEqual(divergencias[0].agendamento_lab_id, agendamento.id)
        self.assertNotEqual(divergencias[0].status, 'conflito')

    def test_livre_no_slot_do_professor_com_reserva_gera_verificar_nunca_conflito(self):
        agendamento = self._agendar(self.turma_a)
        slot_livre = self._slot(situacao='livre')

        divergencias = gerar_divergencias(self.upload, [slot_livre], self.semana_inicio, self.semana_fim)

        self.assertEqual(len(divergencias), 1)
        self.assertEqual(divergencias[0].status, 'verificar')
        self.assertNotEqual(divergencias[0].status, 'conflito')

    def test_comparacao_nao_altera_nenhum_agendamento(self):
        TurmaPdfAlias.objects.create(texto_pdf='3B SIST', turma=self.turma_a)
        self._agendar(self.turma_b)
        antes = list(AgendamentoLab.objects.values('id', 'turma_id', 'data', 'horario', 'turno'))

        gerar_divergencias(self.upload, [self._slot()], self.semana_inicio, self.semana_fim)

        depois = list(AgendamentoLab.objects.values('id', 'turma_id', 'data', 'horario', 'turno'))
        self.assertEqual(antes, depois)

    def test_manha_nao_interfere_na_tarde(self):
        TurmaPdfAlias.objects.create(texto_pdf='3B SIST', turma=self.turma_a)
        agendamento_manha = self._agendar(self.turma_b, turno='manha')
        agendamento_tarde = self._agendar(self.turma_b, turno='tarde')

        divergencias = gerar_divergencias(self.upload, [self._slot()], self.semana_inicio, self.semana_fim)

        agendamentos_referenciados = {d.agendamento_lab_id for d in divergencias}
        self.assertIn(agendamento_manha.id, agendamentos_referenciados)
        self.assertNotIn(agendamento_tarde.id, agendamentos_referenciados)
        self.assertTrue(all(d.turno == 'manha' for d in divergencias))

    def test_tarde_nao_interfere_na_manha(self):
        upload_tarde = HorarioProfessorPDF.objects.create(
            arquivo='horarios_pdf/teste_tarde.pdf', nome_original='teste_tarde.pdf', turno_detectado='tarde',
        )
        TurmaPdfAlias.objects.create(texto_pdf='3B SIST', turma=self.turma_a)
        agendamento_manha = self._agendar(self.turma_b, turno='manha')
        agendamento_tarde = self._agendar(self.turma_b, turno='tarde')

        divergencias = gerar_divergencias(upload_tarde, [self._slot()], self.semana_inicio, self.semana_fim)

        agendamentos_referenciados = {d.agendamento_lab_id for d in divergencias}
        self.assertIn(agendamento_tarde.id, agendamentos_referenciados)
        self.assertNotIn(agendamento_manha.id, agendamentos_referenciados)
        self.assertTrue(all(d.turno == 'tarde' for d in divergencias))


class VerificarHorarioPdfViewsTests(TestCase):
    """Testa o fluxo completo via HTTP: upload, relatorio, analisar e confirmar."""

    def setUp(self):
        self.grupo_equipe = Group.objects.create(name='Equipe Diretiva')
        self.admin = User.objects.create_user(username='diretivo-pdf', password='senha-teste')
        self.admin.groups.add(self.grupo_equipe)
        self.outro_usuario = User.objects.create_user(username='sem-permissao-pdf', password='senha-teste')

        self.laboratorio = Laboratorio.objects.create(nome='Lab. 01', tipo='fixo', equipamento='Computadores', ativo=True)
        self.professor = Professor.objects.create(nome_completo='PROFESSOR TESTE DA SILVA', ativo=True)
        self.turma_a = Turma.objects.create(nome='3B SIST', ano=2026, serie='3 Ano', ativa=True, turno='manha')
        self.turma_b = Turma.objects.create(nome='1A HUM', ano=2026, serie='1 Ano', ativa=True, turno='manha')

        segunda = date(2026, 6, 1)
        self.semana_inicio = segunda - timedelta(days=segunda.weekday())

        ConfiguracaoCronogramaLaboratorio.objects.create(
            data_inicio_cronograma_ativo=self.semana_inicio,
            data_fim_cronograma_ativo=self.semana_inicio + timedelta(days=6),
            data_inicio_manha_ativo=self.semana_inicio,
            data_fim_manha_ativo=self.semana_inicio + timedelta(days=6),
        )

        # Reserva do laboratorio com turma diferente da que o PDF fixture mostra
        # (3B SIST), para forcar um CONFLITO detectavel via o fluxo HTTP completo.
        self.agendamento = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio,
            data=self.semana_inicio,  # segunda-feira
            horario='3',
            turno='manha',
            professor=self.professor,
            turma=self.turma_b,
            disciplina='MATEM.',
            registrado_por=self.admin,
        )

        # Vinculos ja resolvidos (o fluxo de resolver vinculos pendentes e testado
        # separadamente na tela de aliases) para isolar o teste na confirmacao.
        ProfessorPdfAlias.objects.create(texto_pdf='PROFESSOR TESTE', professor=self.professor)
        TurmaPdfAlias.objects.create(texto_pdf='3B SIST', turma=self.turma_a)

        self.client.force_login(self.admin)

    def _upload_fixture(self):
        with open(FIXTURE_PDF_HORARIO_MANHA, 'rb') as arquivo:
            return self.client.post(
                reverse('verificar_horario_pdf_upload'),
                {'arquivo': arquivo, 'turno': ''},
            )

    def test_upload_nao_altera_reservas_existentes(self):
        antes = list(AgendamentoLab.objects.values('id', 'turma_id', 'data', 'horario', 'turno'))

        response = self._upload_fixture()

        self.assertEqual(response.status_code, 302)
        depois = list(AgendamentoLab.objects.values('id', 'turma_id', 'data', 'horario', 'turno'))
        self.assertEqual(antes, depois)

    def test_upload_gera_divergencia_de_conflito(self):
        self._upload_fixture()

        divergencia = DivergenciaCronogramaPdf.objects.get(agendamento_lab=self.agendamento)
        self.assertEqual(divergencia.status, 'conflito')

    def test_consulta_ao_relatorio_nao_altera_reservas(self):
        self._upload_fixture()
        upload = HorarioProfessorPDF.objects.latest('id')
        antes = list(AgendamentoLab.objects.values('id', 'turma_id', 'data', 'horario', 'turno'))

        response = self.client.get(reverse('verificar_horario_pdf_relatorio', args=[upload.id]))

        self.assertEqual(response.status_code, 200)
        depois = list(AgendamentoLab.objects.values('id', 'turma_id', 'data', 'horario', 'turno'))
        self.assertEqual(antes, depois)

    def test_cancelar_confirmacao_nao_altera_nada(self):
        self._upload_fixture()
        divergencia = DivergenciaCronogramaPdf.objects.get(agendamento_lab=self.agendamento)
        antes = (self.agendamento.turma_id, self.agendamento.data, self.agendamento.horario)

        response = self.client.post(
            reverse('verificar_horario_pdf_confirmar', args=[divergencia.id]), {},  # sem 'confirmar': '1'
        )

        self.assertEqual(response.status_code, 302)
        self.agendamento.refresh_from_db()
        self.assertEqual((self.agendamento.turma_id, self.agendamento.data, self.agendamento.horario), antes)
        self.assertFalse(HistoricoAlteracaoCronogramaLab.objects.exists())
        divergencia.refresh_from_db()
        self.assertEqual(divergencia.decisao, 'pendente')

    def test_manter_como_esta_registra_decisao_sem_alterar_reserva(self):
        self._upload_fixture()
        divergencia = DivergenciaCronogramaPdf.objects.get(agendamento_lab=self.agendamento)
        antes = (self.agendamento.turma_id, self.agendamento.data, self.agendamento.horario)

        response = self.client.post(
            reverse('verificar_horario_pdf_analisar', args=[divergencia.id]), {'acao': 'manter'},
        )

        self.assertEqual(response.status_code, 302)
        self.agendamento.refresh_from_db()
        self.assertEqual((self.agendamento.turma_id, self.agendamento.data, self.agendamento.horario), antes)
        divergencia.refresh_from_db()
        self.assertEqual(divergencia.decisao, 'mantido')

    def test_confirmar_altera_somente_a_reserva_selecionada_e_cria_historico(self):
        outro_laboratorio = Laboratorio.objects.create(nome='Lab. 02', tipo='fixo', equipamento='Computadores', ativo=True)
        outro_professor = Professor.objects.create(nome_completo='OUTRO PROFESSOR', ativo=True)
        outro_agendamento = AgendamentoLab.objects.create(
            laboratorio=outro_laboratorio,
            data=self.semana_inicio + timedelta(days=1),
            horario='2',
            turno='manha',
            professor=outro_professor,
            turma=self.turma_b,
            disciplina='HIST.',
            registrado_por=self.admin,
        )
        snapshot_outro = (outro_agendamento.turma_id, outro_agendamento.data, outro_agendamento.horario)

        self._upload_fixture()
        divergencia = DivergenciaCronogramaPdf.objects.get(agendamento_lab=self.agendamento)

        response = self.client.post(
            reverse('verificar_horario_pdf_confirmar', args=[divergencia.id]),
            {
                'confirmar': '1',
                'turma_id': str(self.turma_a.id),
                'dia_semana': 'Segunda-feira',
                'horario': '3',
            },
        )

        self.assertEqual(response.status_code, 302)

        self.agendamento.refresh_from_db()
        self.assertEqual(self.agendamento.turma_id, self.turma_a.id)

        outro_agendamento.refresh_from_db()
        self.assertEqual(
            (outro_agendamento.turma_id, outro_agendamento.data, outro_agendamento.horario),
            snapshot_outro,
        )

        divergencia.refresh_from_db()
        self.assertEqual(divergencia.decisao, 'alterado')

        historico = HistoricoAlteracaoCronogramaLab.objects.get()
        self.assertEqual(historico.usuario, self.admin)
        self.assertEqual(historico.laboratorio_nome, self.laboratorio.nome)
        self.assertEqual(historico.professor_nome, self.professor.nome_completo)
        self.assertEqual(historico.turma_nome_anterior, self.turma_b.nome)
        self.assertEqual(historico.turma_nome_novo, self.turma_a.nome)
        self.assertEqual(historico.dia_anterior, 'Segunda-feira')
        self.assertEqual(historico.dia_novo, 'Segunda-feira')
        self.assertEqual(historico.horario_anterior, '3')
        self.assertEqual(historico.horario_novo, '3')

    def test_usuario_sem_permissao_e_redirecionado_em_todas_as_views(self):
        self._upload_fixture()
        divergencia = DivergenciaCronogramaPdf.objects.filter(agendamento_lab=self.agendamento).first()

        self.client.logout()
        self.client.force_login(self.outro_usuario)

        urls = [
            reverse('verificar_horario_pdf_upload'),
            reverse('verificar_horario_pdf_relatorio', args=[HorarioProfessorPDF.objects.latest('id').id]),
            reverse('verificar_horario_pdf_aliases'),
        ]
        if divergencia:
            urls.append(reverse('verificar_horario_pdf_analisar', args=[divergencia.id]))
            urls.append(reverse('verificar_horario_pdf_confirmar', args=[divergencia.id]))

        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, msg=f'{url} deveria redirecionar usuario sem permissao')


class SemanaCronogramaLaboratorioModelTests(TestCase):
    """Fase 1 da reestrutura de semanas do cronograma de laboratorios: cobre
    somente o model novo (SemanaCronogramaLaboratorio) e a FK nova em
    AgendamentoLab (semana). Nao cobre nenhuma view/logica de negocio - esta
    fase e so a fundacao de dados, nada em producao le/escreve esta tabela
    ainda alem da migration de dados (testada na classe seguinte)."""

    def setUp(self):
        self.user = User.objects.create_user(username='diretivo-semana', password='senha-teste')
        self.turma = Turma.objects.create(nome='2A', ano=2026, serie='2 Ano', ativa=True, turno='manha')
        self.professor = Professor.objects.create(nome_completo='Ana Maria Silva', ativo=True)
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio Semana', tipo='fixo', equipamento='Computadores', ativo=True,
        )

    def test_cria_semana_com_campos_basicos(self):
        semana = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 1, 5), status='editando',
        )
        self.assertEqual(semana.turno, 'manha')
        self.assertEqual(semana.status, 'editando')
        self.assertEqual(semana.data_fim, date(2026, 1, 11))

    def test_data_fim_e_sempre_inicio_mais_seis_dias(self):
        semana = SemanaCronogramaLaboratorio.objects.create(turno='tarde', data_inicio=date(2026, 3, 2))
        self.assertEqual(semana.data_fim, date(2026, 3, 8))

    def test_bloqueia_duplicidade_de_turno_e_data_inicio_entre_editando_e_ativa(self):
        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 1, 5), status='editando',
        )
        with self.assertRaises(IntegrityError):
            SemanaCronogramaLaboratorio.objects.create(
                turno='manha', data_inicio=date(2026, 1, 5), status='ativa',
            )

    def test_permite_duplicidade_de_turno_e_data_inicio_quando_uma_e_historica(self):
        # A constraint so cobre status in ('editando', 'ativa') - uma semana
        # historica ou cancelada pode coexistir com outra semana do mesmo
        # turno+data_inicio sem violar a unicidade.
        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 1, 5), status='historica',
        )
        semana_editando = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 1, 5), status='editando',
        )
        self.assertIsNotNone(semana_editando.pk)

    def test_apenas_uma_semana_ativa_por_turno(self):
        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 1, 5), status='ativa',
        )
        with self.assertRaises(IntegrityError):
            SemanaCronogramaLaboratorio.objects.create(
                turno='manha', data_inicio=date(2026, 2, 2), status='ativa',
            )

    def test_manha_e_tarde_podem_ter_uma_ativa_cada_independentemente(self):
        semana_manha = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 1, 5), status='ativa',
        )
        semana_tarde = SemanaCronogramaLaboratorio.objects.create(
            turno='tarde', data_inicio=date(2026, 1, 5), status='ativa',
        )
        self.assertNotEqual(semana_manha.pk, semana_tarde.pk)
        self.assertEqual(SemanaCronogramaLaboratorio.objects.filter(status='ativa').count(), 2)

    def test_multiplas_semanas_editando_permitidas_no_mesmo_turno(self):
        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 1, 5), status='editando',
        )
        semana_2 = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 1, 12), status='editando',
        )
        self.assertEqual(
            SemanaCronogramaLaboratorio.objects.filter(turno='manha', status='editando').count(), 2,
        )
        self.assertIsNotNone(semana_2.pk)

    def test_fk_semana_em_agendamentolab_e_nullable(self):
        agendamento = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=date(2026, 1, 5), horario='1', turno='manha',
            professor=self.professor, turma=self.turma, disciplina='Matematica',
            registrado_por=self.user,
        )
        self.assertIsNone(agendamento.semana)

    def test_fk_semana_pode_ser_preenchida(self):
        semana = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 1, 5), status='ativa',
        )
        agendamento = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=date(2026, 1, 5), horario='1', turno='manha',
            professor=self.professor, turma=self.turma, disciplina='Matematica',
            registrado_por=self.user, semana=semana,
        )
        agendamento.refresh_from_db()
        self.assertEqual(agendamento.semana_id, semana.id)

    def test_on_delete_protect_bloqueia_exclusao_de_semana_com_agendamentos(self):
        semana = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 1, 5), status='ativa',
        )
        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=date(2026, 1, 5), horario='1', turno='manha',
            professor=self.professor, turma=self.turma, disciplina='Matematica',
            registrado_por=self.user, semana=semana,
        )
        with self.assertRaises(ProtectedError):
            semana.delete()
        # A semana e o agendamento continuam intactos apos a tentativa bloqueada.
        self.assertTrue(SemanaCronogramaLaboratorio.objects.filter(pk=semana.pk).exists())
        self.assertEqual(AgendamentoLab.objects.filter(semana=semana).count(), 1)

    def test_semana_sem_agendamentos_pode_ser_excluida(self):
        semana = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 1, 5), status='cancelada',
        )
        semana.delete()
        self.assertFalse(SemanaCronogramaLaboratorio.objects.filter(pk=semana.pk).exists())


class PovoarSemanaCronogramaLaboratorioMigrationTests(TestCase):
    """Fase 1 - testa a migration de dados 0032 (RunPython) que le o estado
    atual de ConfiguracaoCronogramaLaboratorio e cria/vincula
    SemanaCronogramaLaboratorio, chamando as funcoes da migration
    diretamente (mesmo padrao usado pelo executor de migrations do Django,
    via apps.get_model), sem depender de rodar `migrate` de verdade."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        modulo = importlib.import_module(
            'apps.migrations.0032_povoar_semana_cronograma_laboratorio_fase1'
        )
        # staticmethod evita que o Python trate a funcao como metodo de
        # instancia (o que injetaria `self` como primeiro argumento).
        cls.migrar_forward = staticmethod(modulo.criar_semanas_e_vincular_agendamentos)
        cls.migrar_reverse = staticmethod(modulo.reverter)

    def setUp(self):
        self.user = User.objects.create_user(username='diretivo-migracao', password='senha-teste')
        self.turma = Turma.objects.create(nome='2A', ano=2026, serie='2 Ano', ativa=True, turno='manha')
        self.professor = Professor.objects.create(nome_completo='Ana Maria Silva', ativo=True)
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio Migracao', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        # Config legado ja existente no banco (todo AgendamentoLab de
        # producao roda por cima dele, a migration so le estes campos).
        self.config = ConfiguracaoCronogramaLaboratorio.objects.create(
            data_inicio_cronograma_ativo=date(2020, 1, 1),
            data_fim_cronograma_ativo=date(2020, 1, 7),
            data_inicio_manha_ativo=date(2026, 1, 5),
            data_fim_manha_ativo=date(2026, 1, 11),
            data_inicio_tarde_ativo=date(2026, 1, 5),
            data_fim_tarde_ativo=date(2026, 1, 11),
            data_inicio_manha_editando=date(2026, 1, 19),
            data_inicio_tarde_editando=None,
        )

    def _rodar_forward(self):
        self.migrar_forward(django.apps.apps, schema_editor=None)

    def _rodar_reverse(self):
        self.migrar_reverse(django.apps.apps, schema_editor=None)

    def test_cria_semana_ativa_e_editando_a_partir_da_config(self):
        self._rodar_forward()

        semana_ativa_manha = SemanaCronogramaLaboratorio.objects.get(turno='manha', status='ativa')
        self.assertEqual(semana_ativa_manha.data_inicio, date(2026, 1, 5))
        self.assertIsNotNone(semana_ativa_manha.ativado_em)

        semana_ativa_tarde = SemanaCronogramaLaboratorio.objects.get(turno='tarde', status='ativa')
        self.assertEqual(semana_ativa_tarde.data_inicio, date(2026, 1, 5))

        semana_editando_manha = SemanaCronogramaLaboratorio.objects.get(turno='manha', status='editando')
        self.assertEqual(semana_editando_manha.data_inicio, date(2026, 1, 19))

        # Tarde nao tinha editando configurada -> nao deve ter sido criada.
        self.assertFalse(
            SemanaCronogramaLaboratorio.objects.filter(turno='tarde', status='editando').exists()
        )
        self.assertEqual(SemanaCronogramaLaboratorio.objects.count(), 3)

    def test_nao_altera_nenhum_campo_da_config(self):
        campos_antes = {
            campo.name: getattr(self.config, campo.name)
            for campo in ConfiguracaoCronogramaLaboratorio._meta.fields
        }

        self._rodar_forward()

        self.config.refresh_from_db()
        campos_depois = {
            campo.name: getattr(self.config, campo.name)
            for campo in ConfiguracaoCronogramaLaboratorio._meta.fields
        }
        self.assertEqual(campos_antes, campos_depois)

    def test_vincula_agendamento_dentro_da_semana_ativa(self):
        agendamento = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=date(2026, 1, 8), horario='1', turno='manha',
            professor=self.professor, turma=self.turma, disciplina='Matematica',
            registrado_por=self.user,
        )

        self._rodar_forward()

        agendamento.refresh_from_db()
        semana_ativa = SemanaCronogramaLaboratorio.objects.get(turno='manha', status='ativa')
        self.assertEqual(agendamento.semana_id, semana_ativa.id)

    def test_vincula_agendamento_dentro_da_semana_editando(self):
        agendamento = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=date(2026, 1, 21), horario='1', turno='manha',
            professor=self.professor, turma=self.turma, disciplina='Matematica',
            registrado_por=self.user,
        )

        self._rodar_forward()

        agendamento.refresh_from_db()
        semana_editando = SemanaCronogramaLaboratorio.objects.get(turno='manha', status='editando')
        self.assertEqual(agendamento.semana_id, semana_editando.id)

    def test_agendamento_fora_de_qualquer_semana_conhecida_permanece_null(self):
        agendamento_antigo = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=date(2024, 3, 10), horario='1', turno='manha',
            professor=self.professor, turma=self.turma, disciplina='Historia',
            registrado_por=self.user,
        )

        self._rodar_forward()

        agendamento_antigo.refresh_from_db()
        self.assertIsNone(agendamento_antigo.semana_id)

    def test_agendamento_em_janela_ambigua_permanece_null(self):
        # Se a semana ativa e a semana editando do mesmo turno tiverem
        # janelas de 7 dias que se sobrepoem (nao deveria ocorrer na regra
        # de negocio atual, mas a migration precisa ser defensiva), um
        # agendamento na intersecao nao deve ser vinculado a nenhuma delas.
        self.config.data_inicio_manha_editando = date(2026, 1, 8)  # 08 a 14, sobrepoe com ativa (05-11)
        self.config.save()

        agendamento_ambiguo = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=date(2026, 1, 9), horario='1', turno='manha',
            professor=self.professor, turma=self.turma, disciplina='Matematica',
            registrado_por=self.user,
        )
        agendamento_so_ativa = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=date(2026, 1, 5), horario='2', turno='manha',
            professor=self.professor, turma=self.turma, disciplina='Matematica',
            registrado_por=self.user,
        )
        agendamento_so_editando = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=date(2026, 1, 14), horario='3', turno='manha',
            professor=self.professor, turma=self.turma, disciplina='Matematica',
            registrado_por=self.user,
        )

        self._rodar_forward()

        agendamento_ambiguo.refresh_from_db()
        agendamento_so_ativa.refresh_from_db()
        agendamento_so_editando.refresh_from_db()

        self.assertIsNone(agendamento_ambiguo.semana_id)
        self.assertIsNotNone(agendamento_so_ativa.semana_id)
        self.assertIsNotNone(agendamento_so_editando.semana_id)
        self.assertEqual(
            agendamento_so_ativa.semana.status, 'ativa',
        )
        self.assertEqual(
            agendamento_so_editando.semana.status, 'editando',
        )

    def test_migracao_nao_altera_nenhum_campo_do_agendamento_alem_da_semana(self):
        agendamento = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=date(2026, 1, 8), horario='1', turno='manha',
            professor=self.professor, turma=self.turma, disciplina='Matematica',
            observacao='Levar notebook', registrado_por=self.user,
        )
        campos_a_comparar = [
            campo.name for campo in AgendamentoLab._meta.fields if campo.name != 'semana'
        ]
        antes = {campo: getattr(agendamento, campo) for campo in campos_a_comparar}

        self._rodar_forward()

        agendamento.refresh_from_db()
        depois = {campo: getattr(agendamento, campo) for campo in campos_a_comparar}
        self.assertEqual(antes, depois)
        self.assertIsNotNone(agendamento.semana_id)

    def test_reverter_remove_semanas_e_limpa_fk_sem_apagar_agendamentos(self):
        agendamento = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=date(2026, 1, 8), horario='1', turno='manha',
            professor=self.professor, turma=self.turma, disciplina='Matematica',
            registrado_por=self.user,
        )
        self._rodar_forward()
        agendamento.refresh_from_db()
        self.assertIsNotNone(agendamento.semana_id)
        total_agendamentos_antes = AgendamentoLab.objects.count()

        self._rodar_reverse()

        self.assertEqual(SemanaCronogramaLaboratorio.objects.count(), 0)
        agendamento.refresh_from_db()
        self.assertIsNone(agendamento.semana_id)
        self.assertEqual(AgendamentoLab.objects.count(), total_agendamentos_antes)

    def test_sem_config_nenhuma_nao_cria_nada_e_nao_falha(self):
        self.config.delete()

        self._rodar_forward()  # nao deve lancar excecao

        self.assertEqual(SemanaCronogramaLaboratorio.objects.count(), 0)


class Fase2MultiplasSemanasCronogramaTests(TestCase):
    """Fase 2 da reestrutura do cronograma de laboratorios: cutover para
    SemanaCronogramaLaboratorio como fonte de verdade, com quantidade
    ilimitada de semanas em edicao por turno e ativacao explicita por
    PK/data. Cobre a lista de cenarios da especificacao da Fase 2 (secao 17),
    numerados nos nomes dos testes onde fizer sentido."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='diretivo-fase2', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.user)
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.professor = Professor.objects.create(nome_completo='Helena Prado Nogueira', ativo=True)
        self.turma_manha = Turma.objects.create(nome='1A', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.turma_tarde = Turma.objects.create(nome='6A', ano=2026, serie='6 Ano', ativa=True, turno='tarde')

    # ------------------------------------------------------------- helpers

    def _criar_semana(self, turno, data_inicio):
        return self.client.post(
            reverse('editar_semana_cronograma_laboratorio'),
            {'turno': turno, 'data_inicio': data_inicio.isoformat()},
        )

    def _criar_semana_obj(self, turno, data_inicio):
        self._criar_semana(turno, data_inicio)
        return SemanaCronogramaLaboratorio.objects.get(turno=turno, data_inicio=data_inicio)

    def _ativar_pk(self, semana):
        return self.client.post(
            reverse('ativar_cronograma_laboratorio'),
            {'semana_id': semana.id},
        )

    def _cancelar_pk(self, semana):
        return self.client.post(
            reverse('cancelar_edicao_semana_cronograma_laboratorio'),
            {'semana_id': semana.id},
        )

    def _agendar(self, turno, data, horario='1', turma=None, professor=None, disciplina='Matematica'):
        return AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=data, horario=horario, turno=turno,
            professor=professor or self.professor,
            turma=turma or (self.turma_manha if turno == 'manha' else self.turma_tarde),
            disciplina=disciplina, registrado_por=self.user,
        )

    def _post_grade(self, turno, data_semana, **overrides):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': turno})
        dados = {
            'agendamento_id': '',
            'laboratorio_id': self.laboratorio.id,
            'data': data_semana.isoformat(),
            'horario': '3',
            'turno': turno,
            'professor': self.professor.id,
            'turma': (self.turma_manha if turno == 'manha' else self.turma_tarde).id,
            'disciplina': 'Nova aula',
            'observacao': '',
            'data_inicio': data_semana.isoformat(),
            'laboratorio_filtro': '',
            'scroll_y': '0',
        }
        dados.update(overrides)
        return self.client.post(url, dados)

    # ---------------------------------------------------- 1-4: criar semanas

    def test_1_criar_primeira_semana_em_edicao(self):
        self._criar_semana('manha', date(2026, 1, 5))
        self.assertTrue(SemanaCronogramaLaboratorio.objects.filter(
            turno='manha', data_inicio=date(2026, 1, 5), status='editando',
        ).exists())

    def test_2_criar_segunda_semana_em_edicao(self):
        self._criar_semana('manha', date(2026, 1, 5))
        self._criar_semana('manha', date(2026, 1, 12))
        self.assertEqual(
            SemanaCronogramaLaboratorio.objects.filter(turno='manha', status='editando').count(), 2,
        )

    def test_3_criar_terceira_semana_em_edicao(self):
        for indice in range(3):
            self._criar_semana('manha', date(2026, 1, 5) + timedelta(days=7 * indice))
        self.assertEqual(
            SemanaCronogramaLaboratorio.objects.filter(turno='manha', status='editando').count(), 3,
        )

    def test_4_criar_muitas_semanas_demonstra_ausencia_de_limite_artificial(self):
        total = 12
        for indice in range(total):
            resposta = self._criar_semana('manha', date(2026, 1, 5) + timedelta(days=7 * indice))
            self.assertRedirects(resposta, reverse('diagnostico_cronograma_laboratorios'))
        self.assertEqual(
            SemanaCronogramaLaboratorio.objects.filter(turno='manha', status='editando').count(), total,
        )

    # ------------------------------------------------ 5-7: isolamento/turno

    def test_5_manha_independente_da_tarde(self):
        self._criar_semana('manha', date(2026, 1, 5))
        self._criar_semana('manha', date(2026, 1, 12))
        self.assertEqual(SemanaCronogramaLaboratorio.objects.filter(turno='tarde').count(), 0)

    def test_6_mesma_data_pode_existir_em_manha_e_tarde(self):
        data = date(2026, 1, 5)
        self._criar_semana('manha', data)
        self._criar_semana('tarde', data)
        self.assertTrue(SemanaCronogramaLaboratorio.objects.filter(turno='manha', data_inicio=data).exists())
        self.assertTrue(SemanaCronogramaLaboratorio.objects.filter(turno='tarde', data_inicio=data).exists())

    def test_7_duplicacao_invalida_no_mesmo_turno_e_recusada(self):
        data = date(2026, 1, 5)
        self._criar_semana('manha', data)
        self._criar_semana('manha', data)
        self.assertEqual(
            SemanaCronogramaLaboratorio.objects.filter(turno='manha', data_inicio=data).count(), 1,
        )

    def test_criar_semana_duplicada_da_mensagem_especifica_por_status(self):
        data = date(2026, 1, 5)
        semana_ativa = self._criar_semana_obj('manha', data)
        self._ativar_pk(semana_ativa)

        resposta = self.client.post(
            reverse('editar_semana_cronograma_laboratorio'),
            {'turno': 'manha', 'data_inicio': data.isoformat()},
            follow=True,
        )
        mensagens = [str(m) for m in resposta.context['messages']]
        self.assertTrue(any('ATIVA' in m for m in mensagens))

    def test_ativar_semana_historica_diretamente_e_permitido_e_cancelada_e_bloqueada(self):
        s1 = self._criar_semana_obj('manha', date(2026, 1, 5))
        self._ativar_pk(s1)
        s2 = self._criar_semana_obj('manha', date(2026, 1, 12))
        self._ativar_pk(s2)  # s1 vira historica
        s1.refresh_from_db()
        self.assertEqual(s1.status, 'historica')

        self._ativar_pk(s1)
        s1.refresh_from_db()
        s2.refresh_from_db()
        self.assertEqual(s1.status, 'ativa')
        self.assertEqual(s2.status, 'historica')

        s3 = self._criar_semana_obj('tarde', date(2026, 1, 5))
        self._cancelar_pk(s3)
        self._ativar_pk(s3)  # tenta reativar a cancelada diretamente - deve ser recusado
        s3.refresh_from_db()
        self.assertEqual(s3.status, 'cancelada')

    # --------------------------------------------- 8-11: diagnostico/botoes

    def test_8_diagnostico_lista_todas_as_semanas_em_edicao(self):
        datas = [date(2026, 1, 5), date(2026, 1, 12), date(2026, 1, 19)]
        for data in datas:
            self._criar_semana('manha', data)

        resposta = self.client.get(reverse('diagnostico_cronograma_laboratorios'))
        self.assertEqual(len(resposta.context['semanas_editando_manha']), 3)
        conteudo = resposta.content.decode()
        self.assertEqual(conteudo.count('📝 Em edição'), 3)

    def test_9_cada_semana_editando_tem_seu_proprio_link_editar(self):
        datas = [date(2026, 1, 5), date(2026, 1, 12)]
        for data in datas:
            self._criar_semana('manha', data)

        conteudo = self.client.get(reverse('diagnostico_cronograma_laboratorios')).content.decode()
        url_base = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        for data in datas:
            self.assertIn(f'{url_base}?data_inicio={data.isoformat()}', conteudo)

    def test_10_cada_semana_editando_tem_seu_proprio_botao_ativar_com_data(self):
        datas = [date(2026, 1, 5), date(2026, 1, 12)]
        for data in datas:
            self._criar_semana('manha', data)

        conteudo = self.client.get(reverse('diagnostico_cronograma_laboratorios')).content.decode()
        for data in datas:
            self.assertIn(f'Ativar manhã — {data.strftime("%d/%m")}', conteudo)

    def test_11_botao_de_ativacao_envia_pk_e_data_corretos(self):
        semana = self._criar_semana_obj('manha', date(2026, 1, 5))
        conteudo = self.client.get(reverse('diagnostico_cronograma_laboratorios')).content.decode()
        self.assertIn(f'name="semana_id" value="{semana.id}"', conteudo)
        self.assertIn('name="data_inicio" value="2026-01-05"', conteudo)

    # ------------------------- identidade exata (inicio nao precisa ser segunda)

    def test_criar_23_agosto_preserva_exatamente_a_data_informada(self):
        self._criar_semana('manha', date(2026, 8, 23))

        semana = SemanaCronogramaLaboratorio.objects.get(turno='manha')
        self.assertEqual(semana.data_inicio, date(2026, 8, 23))
        self.assertFalse(
            SemanaCronogramaLaboratorio.objects.filter(
                turno='manha', data_inicio=date(2026, 8, 17),
            ).exists()
        )

    def test_criar_23_agosto_funciona_quando_17_agosto_ja_existe(self):
        self._criar_semana('manha', date(2026, 8, 17))
        self._criar_semana('manha', date(2026, 8, 23))

        self.assertEqual(
            list(SemanaCronogramaLaboratorio.objects.filter(turno='manha').order_by('data_inicio')
                 .values_list('data_inicio', 'status')),
            [
                (date(2026, 8, 17), 'editando'),
                (date(2026, 8, 23), 'editando'),
            ],
        )

    def test_criar_17_agosto_duplicada_continua_bloqueado(self):
        self._criar_semana('manha', date(2026, 8, 17))
        self._criar_semana('manha', date(2026, 8, 17))

        self.assertEqual(
            SemanaCronogramaLaboratorio.objects.filter(
                turno='manha', data_inicio=date(2026, 8, 17),
            ).count(),
            1,
        )

    def test_data_nao_segunda_e_independente_entre_manha_e_tarde(self):
        self._criar_semana('manha', date(2026, 8, 23))
        self._criar_semana('tarde', date(2026, 8, 23))

        self.assertTrue(SemanaCronogramaLaboratorio.objects.filter(
            turno='manha', data_inicio=date(2026, 8, 23), status='editando',
        ).exists())
        self.assertTrue(SemanaCronogramaLaboratorio.objects.filter(
            turno='tarde', data_inicio=date(2026, 8, 23), status='editando',
        ).exists())

    def test_diagnostico_mostra_17_e_23_editando_com_24_historica(self):
        self._criar_semana('manha', date(2026, 8, 17))
        self._criar_semana('manha', date(2026, 8, 23))
        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 8, 24), status='historica',
        )

        resposta = self.client.get(reverse('diagnostico_cronograma_laboratorios'))

        self.assertEqual(
            [semana.data_inicio for semana in resposta.context['semanas_editando_manha']],
            [date(2026, 8, 17), date(2026, 8, 23)],
        )
        self.assertContains(resposta, '17/08/2026')
        self.assertContains(resposta, '23/08/2026')

    def test_admin_a_cria_23_agosto_e_admin_b_ve_a_data_exata(self):
        admin_b = User.objects.create_user(
            username='diretivo-identidade-b', password='senha-teste', is_superuser=True,
        )
        cliente_b = Client()
        cliente_b.force_login(admin_b)

        self._criar_semana('manha', date(2026, 8, 23))
        resposta = cliente_b.get(reverse('diagnostico_cronograma_laboratorios'))

        self.assertEqual(
            [semana.data_inicio for semana in resposta.context['semanas_editando_manha']],
            [date(2026, 8, 23)],
        )

    def test_versao_vazia_com_inicio_23_agosto_aparece_no_diagnostico(self):
        self._criar_semana('manha', date(2026, 8, 23))

        self.assertFalse(AgendamentoLab.objects.exists())
        resposta = self.client.get(reverse('diagnostico_cronograma_laboratorios'))
        self.assertContains(resposta, '23/08/2026')
        self.assertEqual(len(resposta.context['semanas_editando_manha']), 1)

    def test_abertura_da_grade_preserva_data_exata_da_versao(self):
        self._criar_semana('manha', date(2026, 8, 23))
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})

        resposta = self.client.get(url, {'data_inicio': '2026-08-23'})

        self.assertEqual(resposta.context['data_inicio'], date(2026, 8, 23))
        self.assertEqual(resposta.context['data_fim'], date(2026, 8, 29))
        self.assertIn('23/08/2026', resposta.context['cronograma_titulo'])

    def test_ativacao_por_data_usa_identidade_exata_e_nao_a_segunda(self):
        semana_17 = self._criar_semana_obj('manha', date(2026, 8, 17))
        semana_23 = self._criar_semana_obj('manha', date(2026, 8, 23))

        self.client.post(reverse('ativar_cronograma_laboratorio'), {
            'turno': 'manha', 'data_inicio': '2026-08-23',
        })

        semana_17.refresh_from_db()
        semana_23.refresh_from_db()
        self.assertEqual(semana_17.status, 'editando')
        self.assertEqual(semana_23.status, 'ativa')

    def test_grade_associa_dia_interno_a_versao_explicitamente_aberta(self):
        semana = self._criar_semana_obj('manha', date(2026, 8, 23))

        resposta = self._post_grade(
            'manha', date(2026, 8, 23), data='2026-08-24', disciplina='Dia interno',
        )

        self.assertEqual(resposta.status_code, 302)
        agendamento = AgendamentoLab.objects.get(disciplina='Dia interno')
        self.assertEqual(agendamento.data, date(2026, 8, 24))
        self.assertEqual(agendamento.semana_id, semana.id)

    def test_grade_bloqueia_dia_fora_do_intervalo_sem_trocar_de_versao(self):
        self._criar_semana_obj('manha', date(2026, 8, 23))
        self._criar_semana_obj('manha', date(2026, 8, 30))

        self._post_grade(
            'manha', date(2026, 8, 23), data='2026-08-30', disciplina='Fora do intervalo',
        )

        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Fora do intervalo').exists())

    def test_grade_nao_mistura_agendamentos_de_versoes_sobrepostas(self):
        semana_17 = self._criar_semana_obj('manha', date(2026, 8, 17))
        semana_23 = self._criar_semana_obj('manha', date(2026, 8, 23))
        agendamento_17 = self._agendar(
            'manha', date(2026, 8, 23), horario='1', disciplina='Pertence a 17',
        )
        agendamento_17.semana = semana_17
        agendamento_17.save(update_fields=['semana'])
        agendamento_23 = self._agendar(
            'manha', date(2026, 8, 24), horario='2', disciplina='Pertence a 23',
        )
        agendamento_23.semana = semana_23
        agendamento_23.save(update_fields=['semana'])

        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        resposta = self.client.get(url, {'data_inicio': '2026-08-23'})

        self.assertNotContains(resposta, 'Pertence a 17')
        self.assertContains(resposta, 'Pertence a 23')

    def test_criacao_nao_depende_de_segunda_feira_nem_de_mais_seis_dias(self):
        for data_inicio in (date(2026, 8, 17), date(2026, 8, 23), date(2026, 8, 24)):
            self._criar_semana('manha', data_inicio)

        self.assertEqual(
            list(SemanaCronogramaLaboratorio.objects.filter(turno='manha').order_by('data_inicio')
                 .values_list('data_inicio', flat=True)),
            [date(2026, 8, 17), date(2026, 8, 23), date(2026, 8, 24)],
        )

    # --------------------------------------------------- 12-18: ativacao

    def test_12_a_14_ativar_especifica_ativa_so_aquela_e_preserva_as_demais(self):
        antiga_ativa = self._criar_semana_obj('manha', date(2026, 1, 5))
        self._ativar_pk(antiga_ativa)
        antiga_ativa.refresh_from_db()
        self.assertEqual(antiga_ativa.status, 'ativa')

        editando_1 = self._criar_semana_obj('manha', date(2026, 1, 12))
        editando_2 = self._criar_semana_obj('manha', date(2026, 1, 19))

        self._ativar_pk(editando_1)

        antiga_ativa.refresh_from_db()
        editando_1.refresh_from_db()
        editando_2.refresh_from_db()

        self.assertEqual(editando_1.status, 'ativa')       # 12: ativa exatamente aquela
        self.assertEqual(antiga_ativa.status, 'historica')  # 13: antiga ativa vira historica
        self.assertEqual(editando_2.status, 'editando')     # 14: outras editando permanecem

    def test_15_ativacao_da_manha_nao_altera_tarde(self):
        tarde_ativa = self._criar_semana_obj('tarde', date(2026, 1, 5))
        self._ativar_pk(tarde_ativa)

        manha_editando = self._criar_semana_obj('manha', date(2026, 1, 12))
        self._ativar_pk(manha_editando)

        tarde_ativa.refresh_from_db()
        self.assertEqual(tarde_ativa.status, 'ativa')

    def test_16_ativacao_da_tarde_nao_altera_manha(self):
        manha_ativa = self._criar_semana_obj('manha', date(2026, 1, 5))
        self._ativar_pk(manha_ativa)

        tarde_editando = self._criar_semana_obj('tarde', date(2026, 1, 12))
        self._ativar_pk(tarde_editando)

        manha_ativa.refresh_from_db()
        self.assertEqual(manha_ativa.status, 'ativa')

    def test_17_nunca_duas_ativas_no_mesmo_turno(self):
        s1 = self._criar_semana_obj('manha', date(2026, 1, 5))
        self._ativar_pk(s1)
        s2 = self._criar_semana_obj('manha', date(2026, 1, 12))
        self._ativar_pk(s2)
        self.assertEqual(
            SemanaCronogramaLaboratorio.objects.filter(turno='manha', status='ativa').count(), 1,
        )

    def test_18_uma_ativa_de_manha_e_uma_ativa_de_tarde_e_permitido(self):
        semana_manha = self._criar_semana_obj('manha', date(2026, 1, 5))
        semana_tarde = self._criar_semana_obj('tarde', date(2026, 1, 5))
        self._ativar_pk(semana_manha)
        self._ativar_pk(semana_tarde)
        self.assertEqual(SemanaCronogramaLaboratorio.objects.filter(status='ativa').count(), 2)

    # --------------------------------------------------- 19-20: cancelar

    def test_19_cancelar_uma_nao_cancela_as_outras(self):
        s1 = self._criar_semana_obj('manha', date(2026, 1, 5))
        s2 = self._criar_semana_obj('manha', date(2026, 1, 12))
        self._cancelar_pk(s1)
        s1.refresh_from_db()
        s2.refresh_from_db()
        self.assertEqual(s1.status, 'cancelada')
        self.assertEqual(s2.status, 'editando')

    def test_20_cancelar_nao_apaga_agendamentolab(self):
        s1 = self._criar_semana_obj('manha', date(2026, 1, 5))
        agendamento = self._agendar('manha', date(2026, 1, 5))
        agendamento.semana = s1
        agendamento.save(update_fields=['semana'])

        self._cancelar_pk(s1)

        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())
        agendamento.refresh_from_db()
        self.assertEqual(agendamento.semana_id, s1.id)

    # -------------------------------------------- 21-27: protecao de escrita

    def test_21_semana_cancelada_bloqueia_post(self):
        s1 = self._criar_semana_obj('manha', date(2026, 1, 5))
        self._cancelar_pk(s1)

        resposta = self._post_grade('manha', date(2026, 1, 5), disciplina='Nao deveria salvar cancelada')
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Nao deveria salvar cancelada').exists())
        self.assertRedirects(resposta, reverse('diagnostico_cronograma_laboratorios'))

    def test_22_semana_historica_bloqueia_post(self):
        s1 = self._criar_semana_obj('manha', date(2026, 1, 5))
        self._ativar_pk(s1)
        s2 = self._criar_semana_obj('manha', date(2026, 1, 12))
        self._ativar_pk(s2)  # s1 -> historica
        s1.refresh_from_db()
        self.assertEqual(s1.status, 'historica')

        resposta = self._post_grade('manha', date(2026, 1, 5), disciplina='Nao deveria salvar historica')
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Nao deveria salvar historica').exists())
        self.assertRedirects(resposta, reverse('diagnostico_cronograma_laboratorios'))

    def test_23_semana_ativa_aceita_post(self):
        s1 = self._criar_semana_obj('manha', date(2026, 1, 5))
        self._ativar_pk(s1)

        self._post_grade('manha', date(2026, 1, 5), disciplina='Aula na ativa fase2')
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Aula na ativa fase2').exists())

    def test_24_semana_editando_aceita_post(self):
        self._criar_semana_obj('manha', date(2026, 1, 5))

        self._post_grade('manha', date(2026, 1, 5), disciplina='Aula na editando fase2')
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Aula na editando fase2').exists())

    def test_25_semana_inexistente_bloqueia_post(self):
        # Turno 'manha' ja adotado (tem pelo menos 1 linha), mas a data
        # pedida nao corresponde a nenhuma semana cadastrada.
        self._criar_semana_obj('manha', date(2026, 1, 5))

        resposta = self._post_grade('manha', date(2026, 2, 2), disciplina='Semana inexistente')
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Semana inexistente').exists())
        self.assertRedirects(resposta, reverse('diagnostico_cronograma_laboratorios'))

    def test_26_url_arbitraria_bloqueia_post(self):
        self._criar_semana_obj('manha', date(2026, 1, 5))
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})

        resposta = self.client.post(f'{url}?data_inicio=2099-12-27', {
            'agendamento_id': '',
            'laboratorio_id': self.laboratorio.id,
            'data': '2099-12-27',
            'horario': '1',
            'turno': 'manha',
            'professor': self.professor.id,
            'turma': self.turma_manha.id,
            'disciplina': 'Ataque generico',
            'observacao': '',
            'data_inicio': '2099-12-27',
            'laboratorio_filtro': '',
            'scroll_y': '',
        })
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Ataque generico').exists())
        self.assertRedirects(resposta, reverse('diagnostico_cronograma_laboratorios'))

    def test_27_rota_generica_nao_contorna_a_protecao(self):
        s1 = self._criar_semana_obj('manha', date(2026, 1, 5))
        self._cancelar_pk(s1)

        url = reverse('cronograma_semanal')
        self.client.post(url, {
            'agendamento_id': '',
            'laboratorio_id': self.laboratorio.id,
            'data': date(2026, 1, 5).isoformat(),
            'horario': '1',
            'turno': 'manha',
            'professor': self.professor.id,
            'turma': self.turma_manha.id,
            'disciplina': 'Via rota generica',
            'observacao': '',
            'data_inicio': date(2026, 1, 5).isoformat(),
            'turno_filtro': 'manha',
            'laboratorio_filtro': '',
            'scroll_y': '',
        })
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Via rota generica').exists())

    # --------------------------------------------- 28-29: FK em AgendamentoLab

    def test_28_novo_agendamento_recebe_semana_id(self):
        s1 = self._criar_semana_obj('manha', date(2026, 1, 5))

        self._post_grade('manha', date(2026, 1, 5), disciplina='Com semana fase2')

        agendamento = AgendamentoLab.objects.get(disciplina='Com semana fase2')
        self.assertEqual(agendamento.semana_id, s1.id)

    def test_29_edicao_recalcula_e_verifica_a_associacao_de_semana(self):
        s1 = self._criar_semana_obj('manha', date(2026, 1, 5))
        self._post_grade('manha', date(2026, 1, 5), disciplina='Original fase2')
        agendamento = AgendamentoLab.objects.get(disciplina='Original fase2')
        self.assertEqual(agendamento.semana_id, s1.id)

        self._post_grade(
            'manha', date(2026, 1, 5), agendamento_id=agendamento.id, disciplina='Editado fase2',
        )
        agendamento.refresh_from_db()
        self.assertEqual(agendamento.disciplina, 'Editado fase2')
        self.assertEqual(agendamento.semana_id, s1.id)

    # --------------------------------------------- 30-33: PDF/Excel/historico

    def test_30_a_33_pdf_e_excel_funcionam_para_ativa_editando_e_historica(self):
        semana_historica = self._criar_semana_obj('manha', date(2026, 1, 5))
        self._ativar_pk(semana_historica)
        semana_ativa = self._criar_semana_obj('manha', date(2026, 1, 12))
        self._ativar_pk(semana_ativa)  # semana_historica -> historica
        semana_editando = self._criar_semana_obj('manha', date(2026, 1, 19))

        semana_historica.refresh_from_db()
        self.assertEqual(semana_historica.status, 'historica')

        url_print = reverse('cronograma_print')
        url_excel = reverse('exportar_cronograma_excel')

        for semana_obj, rotulo in ((semana_ativa, 'ativa'), (semana_editando, 'editando'), (semana_historica, 'historica')):
            self._agendar('manha', semana_obj.data_inicio, disciplina=f'PDF {rotulo}')

            resposta_pdf = self.client.get(
                url_print, {'data_inicio': semana_obj.data_inicio.isoformat(), 'turno': 'manha'},
            )
            self.assertEqual(resposta_pdf.status_code, 200, f'PDF falhou para status {rotulo}')
            self.assertContains(resposta_pdf, f'PDF {rotulo}')

            resposta_excel = self.client.get(
                url_excel, {'data_inicio': semana_obj.data_inicio.isoformat(), 'turno': 'manha'},
            )
            self.assertEqual(resposta_excel.status_code, 200, f'Excel falhou para status {rotulo}')
            self.assertEqual(
                resposta_excel['Content-Type'],
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            )

    # ------------------------------------- 34-36: compatibilidade temporaria

    def test_34_campos_antigos_ativos_permanecem_sincronizados_temporariamente(self):
        s1 = self._criar_semana_obj('manha', date(2026, 1, 5))
        self._ativar_pk(s1)

        config = ConfiguracaoCronogramaLaboratorio.objects.get()
        self.assertEqual(config.data_inicio_manha_ativo, date(2026, 1, 5))
        self.assertEqual(config.data_fim_manha_ativo, date(2026, 1, 11))

    def test_35_ativacao_da_manha_nao_sobrescreve_campos_antigos_da_tarde(self):
        semana_tarde = self._criar_semana_obj('tarde', date(2026, 2, 2))
        self._ativar_pk(semana_tarde)
        semana_manha = self._criar_semana_obj('manha', date(2026, 1, 5))
        self._ativar_pk(semana_manha)

        config = ConfiguracaoCronogramaLaboratorio.objects.get()
        self.assertEqual(config.data_inicio_tarde_ativo, date(2026, 2, 2))
        self.assertEqual(config.data_inicio_manha_ativo, date(2026, 1, 5))

    def test_36_ativacao_da_tarde_nao_sobrescreve_campos_antigos_da_manha(self):
        semana_manha = self._criar_semana_obj('manha', date(2026, 1, 5))
        self._ativar_pk(semana_manha)
        semana_tarde = self._criar_semana_obj('tarde', date(2026, 2, 2))
        self._ativar_pk(semana_tarde)

        config = ConfiguracaoCronogramaLaboratorio.objects.get()
        self.assertEqual(config.data_inicio_manha_ativo, date(2026, 1, 5))
        self.assertEqual(config.data_inicio_tarde_ativo, date(2026, 2, 2))

    # --------------------------------------------------- 37: concorrencia

    def test_37_tentativa_de_ativacao_concorrente_nao_deixa_duas_ativas(self):
        """SQLite serializa escritores (select_for_update() e no-op nesse
        backend) - a garantia real vem do UPDATE condicional (WHERE
        status='editando') mais a constraint de banco como barreira final.
        Aqui simulamos duas tentativas de ativacao 'quase simultaneas' para
        semanas diferentes do mesmo turno, e tambem um duplo-submit na
        mesma semana."""
        s1 = self._criar_semana_obj('manha', date(2026, 1, 5))
        s2 = self._criar_semana_obj('manha', date(2026, 1, 12))

        self._ativar_pk(s1)
        self._ativar_pk(s2)

        self.assertEqual(
            SemanaCronogramaLaboratorio.objects.filter(turno='manha', status='ativa').count(), 1,
        )

        s3 = self._criar_semana_obj('manha', date(2026, 1, 19))
        self._ativar_pk(s3)
        self._ativar_pk(s3)  # duplo-submit na mesma semana, ja ativa

        self.assertEqual(
            SemanaCronogramaLaboratorio.objects.filter(turno='manha', status='ativa').count(), 1,
        )
        s3.refresh_from_db()
        self.assertEqual(s3.status, 'ativa')

    # ------------------------------------------------ 38: dados da Fase 1

    def test_38_dados_da_fase_1_continuam_validos_sob_a_fase_2(self):
        """Reproduz o estado real deixado pela migracao de dados da Fase 1
        (manha ativa + manha editando + tarde ativa, com AgendamentoLab
        vinculados) e confirma que a Fase 2 opera corretamente sobre ele."""
        semana_ativa_manha = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 8, 24), status='ativa', ativado_em=timezone.now(),
        )
        semana_editando_manha = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 8, 17), status='editando',
        )
        semana_ativa_tarde = SemanaCronogramaLaboratorio.objects.create(
            turno='tarde', data_inicio=date(2026, 8, 24), status='ativa', ativado_em=timezone.now(),
        )

        ag_ativa_manha = self._agendar('manha', date(2026, 8, 24), disciplina='Legado ativa manha')
        ag_ativa_manha.semana = semana_ativa_manha
        ag_ativa_manha.save(update_fields=['semana'])

        ag_editando_manha = self._agendar('manha', date(2026, 8, 17), disciplina='Legado editando manha')
        ag_editando_manha.semana = semana_editando_manha
        ag_editando_manha.save(update_fields=['semana'])

        ag_ativa_tarde = self._agendar('tarde', date(2026, 8, 24), disciplina='Legado ativa tarde')
        ag_ativa_tarde.semana = semana_ativa_tarde
        ag_ativa_tarde.save(update_fields=['semana'])

        resposta = self.client.get(reverse('diagnostico_cronograma_laboratorios'))
        self.assertEqual(resposta.context['semana_ativa_manha_inicio'], date(2026, 8, 24))
        self.assertEqual(len(resposta.context['semanas_editando_manha']), 1)
        self.assertEqual(resposta.context['semana_ativa_tarde_inicio'], date(2026, 8, 24))

        self._post_grade('manha', date(2026, 8, 24), disciplina='Novo na ativa legado')
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Novo na ativa legado').exists())

        self._post_grade('manha', date(2026, 8, 17), disciplina='Novo na editando legado')
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Novo na editando legado').exists())

        for agendamento in (ag_ativa_manha, ag_editando_manha, ag_ativa_tarde):
            self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())


class SemanaAtivaNaoExpiraPorPassagemDeTempoTests(TestCase):
    """Trava formalmente uma regra de negocio da Fase 2 que havia ficado
    ambigua em textos da interface: uma semana com status='ativa' em
    SemanaCronogramaLaboratorio NAO expira pela passagem do tempo.

    data_inicio identifica a semana-base usada para montar a grade daquela
    versao do cronograma; data_fim (= data_inicio + 6 dias, property nao
    persistida) e so um valor derivado para exibir/paginar a grade - nunca e
    comparado contra date.today()/timezone.now() para decidir se o
    cronograma continua sendo o ativo. O UNICO jeito de uma 'ativa' deixar
    de ser a ativa e: (a) outra semana do MESMO turno ser ativada
    explicitamente (a anterior vira 'historica'), ou (b) o turno ser
    desativado explicitamente (vira 'historica' tambem) - nunca por tempo.
    Isso foi confirmado por auditoria de codigo (obter_cronograma_ativo,
    _estado_semana_para_exibicao, a protecao de escrita em
    cronograma_semanal, e _ativar_semana_por_pk nao fazem nenhuma
    comparacao de data atual para essa decisao) e esta classe fixa essa
    garantia com testes, sem ter sido necessario alterar nenhuma logica -
    so os textos da interface que mencionavam "duracao de 7 dias" de forma
    ambigua."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='diretivo-validade', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.user)
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.professor = Professor.objects.create(nome_completo='Isadora Melo Franco', ativo=True)
        self.turma_manha = Turma.objects.create(nome='1A', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.turma_tarde = Turma.objects.create(nome='6A', ano=2026, serie='6 Ano', ativa=True, turno='tarde')
        # Semana-base bem distante do "hoje" real do servidor (varios anos
        # no passado) - se qualquer logica comparasse contra date.today(),
        # este cronograma pareceria "expirado"; os testes abaixo provam que
        # isso nao acontece, sem depender de mock/freeze de tempo.
        self.data_base_antiga = date(2020, 1, 6)  # segunda-feira

    def _agendar(self, turno, data, horario='1', turma=None, disciplina='Matematica'):
        return AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=data, horario=horario, turno=turno,
            professor=self.professor,
            turma=turma or (self.turma_manha if turno == 'manha' else self.turma_tarde),
            disciplina=disciplina, registrado_por=self.user,
        )

    def test_cronograma_ativo_continua_ativo_muito_depois_da_semana_base(self):
        dias_desde_a_base = (date.today() - self.data_base_antiga).days
        self.assertGreater(dias_desde_a_base, 7, 'o cenario testado precisa estar "muito depois" da semana-base')

        semana = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=self.data_base_antiga, status='ativa', ativado_em=timezone.now(),
        )

        ativo_inicio, ativo_fim = obter_cronograma_ativo('manha')
        self.assertEqual(ativo_inicio, self.data_base_antiga)
        self.assertEqual(ativo_fim, self.data_base_antiga + timedelta(days=6))

        semana.refresh_from_db()
        self.assertEqual(semana.status, 'ativa')

    def test_passagem_do_tempo_nao_altera_status_automaticamente(self):
        semana = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=self.data_base_antiga, status='ativa',
        )
        # Nao existe job nem side-effect de tempo nesta fase - reconferir
        # repetidamente sem nenhuma acao deve sempre dar o mesmo resultado.
        for _ in range(3):
            semana.refresh_from_db()
            self.assertEqual(semana.status, 'ativa')

        ativo_inicio, _ = obter_cronograma_ativo('manha')
        self.assertEqual(ativo_inicio, self.data_base_antiga)

    def test_somente_ativar_outra_semana_do_mesmo_turno_torna_a_anterior_historica(self):
        antiga = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=self.data_base_antiga, status='ativa',
        )
        antiga.refresh_from_db()
        self.assertEqual(antiga.status, 'ativa', 'ainda ativa mesmo muito depois da sua semana-base')

        nova = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 1, 5), status='editando',
        )
        self.client.post(reverse('ativar_cronograma_laboratorio'), {'semana_id': nova.id})

        antiga.refresh_from_db()
        nova.refresh_from_db()
        self.assertEqual(antiga.status, 'historica')  # so por ter sido substituida, nunca por tempo
        self.assertEqual(nova.status, 'ativa')

    def test_manha_antiga_nao_interfere_na_tarde_mesmo_ativa_ha_muito_tempo(self):
        semana_manha = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=self.data_base_antiga, status='ativa',
        )
        semana_tarde = SemanaCronogramaLaboratorio.objects.create(
            turno='tarde', data_inicio=date(2026, 1, 5), status='editando',
        )

        self.client.post(reverse('ativar_cronograma_laboratorio'), {'semana_id': semana_tarde.id})

        semana_manha.refresh_from_db()
        semana_tarde.refresh_from_db()
        self.assertEqual(semana_manha.status, 'ativa')  # manha, "antiga", continua intacta
        self.assertEqual(semana_tarde.status, 'ativa')

    def test_nenhuma_comparacao_com_data_atual_decide_semana_ativa(self):
        """Reforca em codigo, por inspecao estatica do source das funcoes
        que DECIDEM se uma semana e a ativa/qual estado exibir, a garantia
        auditada manualmente: nenhuma delas usa
        date.today()/timezone.now()/datetime.now() para essa decisao.

        Nao inclui _ativar_semana_por_pk aqui de proposito: essa funcao usa
        timezone.now() legitimamente para GRAVAR o timestamp de auditoria
        ativado_em (quando a ativacao aconteceu), o que nao tem nenhuma
        relacao com decidir se uma semana "continua" ativa - isso e testado
        separadamente pelos demais testes desta classe (a ativa so muda de
        status por outra ativacao explicita, nunca por tempo)."""
        import inspect
        from apps.views import laboratorios as views_laboratorios

        funcoes_que_decidem_status_ativo = [
            views_laboratorios.obter_cronograma_ativo,
            views_laboratorios._estado_semana_para_exibicao,
            views_laboratorios._semana_ativa_obj,
        ]
        for funcao in funcoes_que_decidem_status_ativo:
            codigo = inspect.getsource(funcao)
            self.assertNotIn('date.today()', codigo, f'{funcao.__name__} nao deveria usar date.today()')
            self.assertNotIn('timezone.now()', codigo, f'{funcao.__name__} nao deveria usar timezone.now() para decidir status')
            self.assertNotIn('datetime.now()', codigo, f'{funcao.__name__} nao deveria usar datetime.now()')

    def test_diagnostico_mostra_como_ativo_cronograma_com_semana_base_de_anos_atras(self):
        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=self.data_base_antiga, status='ativa',
        )

        resposta = self.client.get(reverse('diagnostico_cronograma_laboratorios'))
        self.assertEqual(resposta.context['semana_ativa_manha_inicio'], self.data_base_antiga)
        self.assertContains(resposta, f'desde {self.data_base_antiga.strftime("%d/%m/%Y")}')
        self.assertContains(resposta, 'CRONOGRAMA ATIVO')

    def test_grade_da_semana_ativa_antiga_continua_aceitando_escrita(self):
        """Alem de continuar 'ativa' nos dados, a grade continua aceitando
        POST normalmente para essa semana - a protecao de escrita tambem
        nao depende de quao antiga e a semana-base."""
        SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=self.data_base_antiga, status='ativa',
        )
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        resposta = self.client.post(url, {
            'agendamento_id': '',
            'laboratorio_id': self.laboratorio.id,
            'data': self.data_base_antiga.isoformat(),
            'horario': '1',
            'turno': 'manha',
            'professor': self.professor.id,
            'turma': self.turma_manha.id,
            'disciplina': 'Aula na ativa antiga',
            'observacao': '',
            'data_inicio': self.data_base_antiga.isoformat(),
            'laboratorio_filtro': '',
            'scroll_y': '',
        })
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Aula na ativa antiga').exists())


class ConsultaCronogramaSomenteLeituraTests(TestCase):
    """Perfil somente leitura (grupo 'Consulta Cronograma'): pode consultar
    o cronograma de laboratorios (manha/tarde, ativa, historica, PDF,
    Excel), mas nao pode alterar absolutamente nada. A protecao real esta
    no backend (decorators/checagens explicitas) - os botoes de escrita
    tambem nao aparecem no HTML, mas isso e so reforco de interface."""

    def setUp(self):
        self.grupo_consulta = Group.objects.create(name='Consulta Cronograma')
        self.usuario_consulta = User.objects.create_user(
            username='consulta-cronograma-teste', password='senha-teste',
        )
        self.usuario_consulta.groups.add(self.grupo_consulta)
        self.client.force_login(self.usuario_consulta)

        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio 1', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.professor = Professor.objects.create(nome_completo='Julia Antunes Cardoso', ativo=True)
        self.turma_manha = Turma.objects.create(nome='1A', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.turma_tarde = Turma.objects.create(nome='6A', ano=2026, serie='6 Ano', ativa=True, turno='tarde')

        self.semana_ativa_manha = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 8, 24), status='ativa', ativado_em=timezone.now(),
        )
        self.semana_historica_manha = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 1, 5), status='historica',
        )
        self.semana_editando_manha = SemanaCronogramaLaboratorio.objects.create(
            turno='manha', data_inicio=date(2026, 9, 7), status='editando',
        )
        self.semana_ativa_tarde = SemanaCronogramaLaboratorio.objects.create(
            turno='tarde', data_inicio=date(2026, 8, 24), status='ativa', ativado_em=timezone.now(),
        )

        self.agendamento_ativa_manha = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=date(2026, 8, 24), horario='1', turno='manha',
            professor=self.professor, turma=self.turma_manha, disciplina='Matematica Consulta',
            registrado_por=self.usuario_consulta, semana=self.semana_ativa_manha,
        )
        self.agendamento_historica_manha = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=date(2026, 1, 5), horario='1', turno='manha',
            professor=self.professor, turma=self.turma_manha, disciplina='Historica Consulta',
            registrado_por=self.usuario_consulta, semana=self.semana_historica_manha,
        )
        self.agendamento_ativa_tarde = AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=date(2026, 8, 24), horario='1', turno='tarde',
            professor=self.professor, turma=self.turma_tarde, disciplina='Historia Consulta',
            registrado_por=self.usuario_consulta, semana=self.semana_ativa_tarde,
        )

    # -------------------------------------------------------- 1-6: consulta

    def test_1_visualiza_manha(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        resposta = self.client.get(url, {'data_inicio': '2026-08-24'})
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'Matematica Consulta')

    def test_2_visualiza_tarde(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'})
        resposta = self.client.get(url, {'data_inicio': '2026-08-24'})
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'Historia Consulta')

    def test_3_visualiza_semana_ativa(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        resposta = self.client.get(url, {'data_inicio': '2026-08-24'})
        self.assertIn('Cronograma Ativo', resposta.context['cronograma_titulo'])

    def test_4_visualiza_semana_historica(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        resposta = self.client.get(url, {'data_inicio': '2026-01-05'})
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'Historica Consulta')

    def test_5_gera_abre_pdf(self):
        url = reverse('cronograma_print')
        resposta = self.client.get(url, {'data_inicio': '2026-08-24', 'turno': 'manha'})
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'Matematica Consulta')

    def test_6_exporta_excel(self):
        url = reverse('exportar_cronograma_excel')
        resposta = self.client.get(url, {'data_inicio': '2026-08-24', 'turno': 'manha'})
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(
            resposta['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

    def test_consulta_cronograma_entrada_de_leitura_funciona(self):
        resposta = self.client.get(reverse('consulta_cronograma_laboratorios'))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'desde 24/08/2026')

    # --------------------------------------------------- 7-11: sem escrita

    def test_7_nao_consegue_criar_semana(self):
        resposta = self.client.post(
            reverse('editar_semana_cronograma_laboratorio'),
            {'turno': 'manha', 'data_inicio': '2026-10-05'},
        )
        self.assertNotEqual(resposta.status_code, 200)
        self.assertFalse(
            SemanaCronogramaLaboratorio.objects.filter(turno='manha', data_inicio=date(2026, 10, 5)).exists()
        )

    def test_8_nao_consegue_ativar(self):
        resposta = self.client.post(
            reverse('ativar_cronograma_laboratorio'),
            {'semana_id': self.semana_editando_manha.id},
        )
        self.assertNotEqual(resposta.status_code, 200)
        self.semana_editando_manha.refresh_from_db()
        self.assertEqual(self.semana_editando_manha.status, 'editando')

    def test_9_nao_consegue_desativar(self):
        resposta = self.client.post(
            reverse('desativar_cronograma_laboratorio'),
            {'turno': 'manha'},
        )
        self.assertNotEqual(resposta.status_code, 200)
        self.semana_ativa_manha.refresh_from_db()
        self.assertEqual(self.semana_ativa_manha.status, 'ativa')

    def test_10_nao_consegue_cancelar(self):
        resposta = self.client.post(
            reverse('cancelar_edicao_semana_cronograma_laboratorio'),
            {'semana_id': self.semana_editando_manha.id},
        )
        self.assertNotEqual(resposta.status_code, 200)
        self.semana_editando_manha.refresh_from_db()
        self.assertEqual(self.semana_editando_manha.status, 'editando')

    def test_11_nao_consegue_excluir_semana(self):
        resposta = self.client.post(
            reverse('excluir_semana_cronograma_laboratorio'),
            {'turno': 'manha', 'data_inicio': '2026-01-05'},
        )
        self.assertNotEqual(resposta.status_code, 200)
        self.assertTrue(AgendamentoLab.objects.filter(id=self.agendamento_historica_manha.id).exists())

    # ----------------------------------------- 12-16: grade travada por turno

    def test_12_nao_consegue_cadastrar_agendamento(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        total_antes = AgendamentoLab.objects.count()

        resposta = self.client.post(url, {
            'agendamento_id': '',
            'laboratorio_id': self.laboratorio.id,
            'data': '2026-08-24',
            'horario': '3',
            'turno': 'manha',
            'professor': self.professor.id,
            'turma': self.turma_manha.id,
            'disciplina': 'Tentativa consulta',
            'observacao': '',
            'data_inicio': '2026-08-24',
            'laboratorio_filtro': '',
            'scroll_y': '',
        })

        self.assertEqual(AgendamentoLab.objects.count(), total_antes)
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Tentativa consulta').exists())
        # So confirma o redirecionamento em si (sem seguir): o destino
        # (diagnostico) tambem e bloqueado para este grupo, entao seguir o
        # redirect resultaria em outro 302, nao em 200.
        self.assertEqual(resposta.status_code, 302)
        self.assertEqual(resposta.url, reverse('diagnostico_cronograma_laboratorios'))

    def test_13_nao_consegue_alterar_agendamento_existente(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})

        self.client.post(url, {
            'agendamento_id': self.agendamento_ativa_manha.id,
            'laboratorio_id': self.laboratorio.id,
            'data': '2026-08-24',
            'horario': '1',
            'turno': 'manha',
            'professor': self.professor.id,
            'turma': self.turma_manha.id,
            'disciplina': 'Alterado indevidamente',
            'observacao': '',
            'data_inicio': '2026-08-24',
            'laboratorio_filtro': '',
            'scroll_y': '',
        })

        self.agendamento_ativa_manha.refresh_from_db()
        self.assertEqual(self.agendamento_ativa_manha.disciplina, 'Matematica Consulta')

    def test_14_post_fabricado_com_semana_id_valido_e_recusado(self):
        """Mesmo um POST 'bem formado', com o id real de uma semana em
        edicao (que aceitaria escrita para Equipe Diretiva), e recusado -
        a checagem de grupo vem antes de qualquer outra logica."""
        resposta = self.client.post(
            reverse('ativar_cronograma_laboratorio'),
            {'turno': 'manha', 'data_inicio': '2026-09-07', 'semana_id': self.semana_editando_manha.id},
        )
        self.assertNotEqual(resposta.status_code, 200)
        self.semana_editando_manha.refresh_from_db()
        self.assertEqual(self.semana_editando_manha.status, 'editando')

    def test_15_url_direta_de_exclusao_de_agendamento_e_recusada(self):
        url = reverse('excluir_agendamento', args=[self.agendamento_ativa_manha.id])
        resposta = self.client.get(url)
        self.assertNotEqual(resposta.status_code, 200)
        self.assertTrue(AgendamentoLab.objects.filter(id=self.agendamento_ativa_manha.id).exists())

    def test_16_botoes_de_escrita_nao_aparecem_no_html(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        resposta = self.client.get(url, {'data_inicio': '2026-08-24'})
        conteudo = resposta.content.decode()

        self.assertFalse(resposta.context['pode_editar_grade'])
        self.assertNotIn('Cadastrar', conteudo)
        self.assertNotIn('fa-trash-alt', conteudo)
        self.assertNotIn('id="agendamentoModal"', conteudo)
        self.assertNotIn('agendamentoModalForm', conteudo)

        # O Diagnostico administrativo (com Criar semana/Ativar/Cancelar/
        # Excluir) tambem nao e acessivel para este grupo.
        resposta_diagnostico = self.client.get(reverse('diagnostico_cronograma_laboratorios'))
        self.assertNotEqual(resposta_diagnostico.status_code, 200)

    # -------------------------------------------- 17-18: nenhum outro modulo

    def test_17_nao_ganha_acesso_ao_django_admin(self):
        resposta = self.client.get('/admin/')
        self.assertNotEqual(resposta.status_code, 200)
        self.assertFalse(self.usuario_consulta.is_staff)

    def test_18_nao_ganha_acesso_a_outros_modulos_administrativos(self):
        resposta_painel = self.client.get(reverse('painel_equipe'))
        self.assertNotEqual(resposta_painel.status_code, 200)

        resposta_faltas = self.client.get(reverse('controle_faltas'))
        self.assertNotEqual(resposta_faltas.status_code, 200)

        resposta_listar_labs = self.client.get(reverse('listar_laboratorios'))
        self.assertNotEqual(resposta_listar_labs.status_code, 200)

    # ------------------------------------------- 19: sem regressao na equipe

    def test_19_equipe_diretiva_continua_funcionando_sem_regressao(self):
        grupo_equipe = Group.objects.create(name='Equipe Diretiva')
        admin = User.objects.create_user(username='diretivo-regressao-consulta', password='senha-teste')
        admin.groups.add(grupo_equipe)
        cliente_admin = Client()
        cliente_admin.force_login(admin)

        resposta_diagnostico = cliente_admin.get(reverse('diagnostico_cronograma_laboratorios'))
        self.assertEqual(resposta_diagnostico.status_code, 200)

        resposta_grade = cliente_admin.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}),
            {'data_inicio': '2026-08-24'},
        )
        self.assertEqual(resposta_grade.status_code, 200)
        self.assertTrue(resposta_grade.context['pode_editar_grade'])

        cliente_admin.post(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'}),
            {
                'agendamento_id': '',
                'laboratorio_id': self.laboratorio.id,
                'data': '2026-08-24',
                'horario': '5',
                'turno': 'manha',
                'professor': self.professor.id,
                'turma': self.turma_manha.id,
                'disciplina': 'Equipe diretiva ok',
                'observacao': '',
                'data_inicio': '2026-08-24',
                'laboratorio_filtro': '',
                'scroll_y': '',
            },
        )
        self.assertTrue(AgendamentoLab.objects.filter(disciplina='Equipe diretiva ok').exists())

        cliente_admin.post(
            reverse('ativar_cronograma_laboratorio'), {'semana_id': self.semana_editando_manha.id},
        )
        self.semana_editando_manha.refresh_from_db()
        self.assertEqual(self.semana_editando_manha.status, 'ativa')


class HistoricoNoDiagnosticoCronogramaTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(
            username='diretivo-historico-diagnostico', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.usuario)
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio Historico', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.professor = Professor.objects.create(nome_completo='Professor Historico', ativo=True)
        self.turma_manha = Turma.objects.create(
            nome='4A', ano=2026, serie='4 Ano', ativa=True, turno='manha',
        )
        self.turma_tarde = Turma.objects.create(
            nome='9A', ano=2026, serie='9 Ano', ativa=True, turno='tarde',
        )
        self.historica_manha_antiga = self._semana('manha', date(2026, 8, 10), 'historica')
        self.historica_manha = self._semana('manha', date(2026, 8, 24), 'historica')
        self.historica_tarde = self._semana('tarde', date(2026, 8, 17), 'historica')
        self.ativa_manha = self._semana('manha', date(2026, 8, 17), 'ativa')
        self.editando_manha = self._semana('manha', date(2026, 8, 31), 'editando')
        self.cancelada_tarde = self._semana('tarde', date(2026, 8, 31), 'cancelada')

        self._agendar(self.historica_manha, '1', 'Historica alvo')
        self._agendar(self.historica_manha, '2', 'Historica segundo horario')
        self._agendar(self.historica_manha_antiga, '1', 'Historica antiga')
        self._agendar(self.historica_tarde, '1', 'Historica tarde')
        self._agendar(self.ativa_manha, '1', 'Ativa atual')

    def _semana(self, turno, data_inicio, status):
        return SemanaCronogramaLaboratorio.objects.create(
            turno=turno, data_inicio=data_inicio, status=status,
        )

    def _agendar(self, semana, horario, disciplina):
        return AgendamentoLab.objects.create(
            laboratorio=self.laboratorio,
            data=semana.data_inicio,
            horario=horario,
            turno=semana.turno,
            professor=self.professor,
            turma=self.turma_manha if semana.turno == 'manha' else self.turma_tarde,
            disciplina=disciplina,
            registrado_por=self.usuario,
            semana=semana,
        )

    def _diagnostico(self):
        return self.client.get(reverse('diagnostico_cronograma_laboratorios'))

    def _html_secao(self, response, turno):
        html = response.content.decode()
        inicio = html.index(f'data-historico-turno="{turno}"')
        outro_turno = 'tarde' if turno == 'manha' else None
        if outro_turno:
            fim = html.index(f'data-historico-turno="{outro_turno}"', inicio)
            return html[inicio:fim]
        return html[inicio:html.index('</section>', inicio)]

    def test_historicas_aparecem_separadas_por_manha_e_tarde(self):
        response = self._diagnostico()

        self.assertContains(response, 'Histórico — Manhã')
        self.assertContains(response, 'Histórico — Tarde')
        self.assertIn(self.historica_manha, response.context['semanas_historicas_manha'])
        self.assertIn(self.historica_tarde, response.context['semanas_historicas_tarde'])

    def test_multiplas_historicas_sao_ordenadas_da_mais_recente_para_a_mais_antiga(self):
        response = self._diagnostico()
        datas = [semana.data_inicio for semana in response.context['semanas_historicas_manha']]

        self.assertEqual(datas, [date(2026, 8, 24), date(2026, 8, 10)])
        secao = self._html_secao(response, 'manha')
        self.assertLess(secao.index('24/08/2026'), secao.index('10/08/2026'))

    def test_lista_historica_exclui_ativa_editando_e_cancelada(self):
        response = self._diagnostico()
        ids_historicos = {
            semana.id
            for semana in (
                *response.context['semanas_historicas_manha'],
                *response.context['semanas_historicas_tarde'],
            )
        }

        self.assertNotIn(self.ativa_manha.id, ids_historicos)
        self.assertNotIn(self.editando_manha.id, ids_historicos)
        self.assertNotIn(self.cancelada_tarde.id, ids_historicos)

    def test_historica_mostra_data_e_quantidade_de_horarios(self):
        response = self._diagnostico()
        historica = response.context['semanas_historicas_manha'][0]

        self.assertEqual(historica.data_inicio, date(2026, 8, 24))
        self.assertEqual(historica.horarios_total, 2)
        secao = self._html_secao(response, 'manha')
        self.assertIn('24/08/2026', secao)
        self.assertIn('2 horários', secao)

    def test_historico_nao_exibe_links_de_visualizacao_pdf_ou_excel(self):
        response = self._diagnostico()
        secao = self._html_secao(response, 'manha')

        self.assertNotIn('Visualizar', secao)
        self.assertNotIn('Imprimir / PDF', secao)
        self.assertNotIn('Excel', secao)

    def test_secao_historica_oferece_somente_editar_ativar_e_remover(self):
        response = self._diagnostico()

        for turno in ('manha', 'tarde'):
            secao = self._html_secao(response, turno)
            self.assertEqual(secao.count('<form'), secao.count('</form>'))
            self.assertIn('✏️ Editar', secao)
            self.assertIn('▶️ Ativar', secao)
            self.assertIn('🗑️ Remover', secao)
            self.assertNotIn('Visualizar', secao)
            self.assertNotIn('Reativar', secao)
            self.assertNotIn('Cancelar', secao)
            self.assertNotIn('Excluir', secao)

    def test_botao_editar_historico_usa_confirmacao_e_pk_da_versao(self):
        response = self._diagnostico()
        secao_manha = self._html_secao(response, 'manha')
        secao_tarde = self._html_secao(response, 'tarde')
        url_editar = reverse('editar_semana_cronograma_laboratorio')

        self.assertIn('✏️ Editar', secao_manha)
        self.assertIn('versão histórica da manhã de 24/08 em edição?', secao_manha)
        self.assertIn(f'name="semana_id" value="{self.historica_manha.id}"', secao_manha)
        self.assertIn(f'action="{url_editar}"', secao_manha)
        self.assertIn('✏️ Editar', secao_tarde)
        self.assertIn('versão histórica da tarde de 17/08 em edição?', secao_tarde)
        self.assertIn(f'name="semana_id" value="{self.historica_tarde.id}"', secao_tarde)

    def test_historica_vira_editando_sem_alterar_ativa_turnos_ou_outras_versoes(self):
        outra_historica = self.historica_manha_antiga
        editando = self.editando_manha
        historica_tarde = self.historica_tarde

        response = self.client.post(
            reverse('editar_semana_cronograma_laboratorio'), {'semana_id': self.historica_manha.id},
        )

        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))
        for semana in (
            self.historica_manha, self.ativa_manha, outra_historica, editando, historica_tarde,
        ):
            semana.refresh_from_db()
        self.assertEqual(self.historica_manha.status, 'editando')
        self.assertEqual(self.ativa_manha.status, 'ativa')
        self.assertEqual(outra_historica.status, 'historica')
        self.assertEqual(editando.status, 'editando')
        self.assertEqual(historica_tarde.status, 'historica')
        self.assertEqual(SemanaCronogramaLaboratorio.objects.filter(turno='manha', status='ativa').count(), 1)

    def test_promocao_para_edicao_preserva_agendamentos_e_semana_id(self):
        antes = list(
            AgendamentoLab.objects.order_by('id').values_list(
                'id', 'semana_id', 'data', 'turno', 'horario', 'disciplina',
            )
        )

        self.client.post(
            reverse('editar_semana_cronograma_laboratorio'), {'semana_id': self.historica_manha.id},
        )

        depois = list(
            AgendamentoLab.objects.order_by('id').values_list(
                'id', 'semana_id', 'data', 'turno', 'horario', 'disciplina',
            )
        )
        self.assertEqual(depois, antes)

    def test_editar_historica_da_manha_nao_altera_ativa_da_tarde(self):
        ativa_tarde = self._semana('tarde', date(2026, 8, 24), 'ativa')

        self.client.post(
            reverse('editar_semana_cronograma_laboratorio'), {'semana_id': self.historica_manha.id},
        )

        ativa_tarde.refresh_from_db()
        self.assertEqual(ativa_tarde.status, 'ativa')
        self.assertEqual(
            SemanaCronogramaLaboratorio.objects.filter(turno='tarde', status='ativa').count(), 1,
        )

    def test_ativar_versao_ja_ativa_e_operacao_neutra(self):
        antes_semanas = list(
            SemanaCronogramaLaboratorio.objects.order_by('id').values_list(
                'id', 'status', 'ativado_por_id', 'ativado_em',
            )
        )
        antes_agendamentos = list(
            AgendamentoLab.objects.order_by('id').values_list('id', 'semana_id'),
        )

        self.client.post(
            reverse('ativar_cronograma_laboratorio'), {'semana_id': self.ativa_manha.id},
        )

        self.assertEqual(
            list(SemanaCronogramaLaboratorio.objects.order_by('id').values_list(
                'id', 'status', 'ativado_por_id', 'ativado_em',
            )),
            antes_semanas,
        )
        self.assertEqual(
            list(AgendamentoLab.objects.order_by('id').values_list('id', 'semana_id')),
            antes_agendamentos,
        )

    def test_cancelada_nao_pode_voltar_para_edicao_por_essa_acao(self):
        self.client.post(
            reverse('editar_semana_cronograma_laboratorio'), {'semana_id': self.cancelada_tarde.id},
        )

        self.cancelada_tarde.refresh_from_db()
        self.assertEqual(self.cancelada_tarde.status, 'cancelada')

    def test_consulta_cronograma_nao_edita_historica_por_url_direta(self):
        grupo = Group.objects.create(name='Consulta Cronograma')
        usuario = User.objects.create_user(username='consulta-nao-ativa-historico', password='senha-teste')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        response = self.client.post(
            reverse('editar_semana_cronograma_laboratorio'), {'semana_id': self.historica_manha.id},
        )

        self.assertEqual(response.status_code, 302)
        self.historica_manha.refresh_from_db()
        self.ativa_manha.refresh_from_db()
        self.assertEqual(self.historica_manha.status, 'historica')
        self.assertEqual(self.ativa_manha.status, 'ativa')

    def test_edicao_por_pk_ignora_turno_e_data_forjados(self):
        self.client.post(reverse('editar_semana_cronograma_laboratorio'), {
            'semana_id': self.historica_manha.id,
            'turno': 'tarde',
            'data_inicio': '2099-12-27',
        })

        self.historica_manha.refresh_from_db()
        self.assertEqual(self.historica_manha.status, 'editando')

    def test_edicao_nao_depende_da_idade_da_versao(self):
        muito_antiga = self._semana('tarde', date(2000, 1, 3), 'historica')
        ativa_tarde = self._semana('tarde', date(2026, 8, 24), 'ativa')

        self.client.post(
            reverse('editar_semana_cronograma_laboratorio'), {'semana_id': muito_antiga.id},
        )

        muito_antiga.refresh_from_db()
        ativa_tarde.refresh_from_db()
        self.assertEqual(muito_antiga.status, 'editando')
        self.assertEqual(ativa_tarde.status, 'ativa')

    def test_conflito_com_editando_na_mesma_data_reverte_toda_a_transacao(self):
        historica_conflitante = self._semana('manha', self.editando_manha.data_inicio, 'historica')

        response = self.client.post(
            reverse('editar_semana_cronograma_laboratorio'), {'semana_id': historica_conflitante.id},
            follow=True,
        )

        historica_conflitante.refresh_from_db()
        self.editando_manha.refresh_from_db()
        self.ativa_manha.refresh_from_db()
        self.assertEqual(historica_conflitante.status, 'historica')
        self.assertEqual(self.editando_manha.status, 'editando')
        self.assertEqual(self.ativa_manha.status, 'ativa')
        mensagens = [str(message) for message in response.context['messages']]
        self.assertTrue(any('Nenhuma alteracao foi aplicada' in message for message in mensagens))

    def test_conflito_com_ativa_ou_outra_historica_tambem_bloqueia(self):
        historica_mesma_data_ativa = self._semana('manha', self.ativa_manha.data_inicio, 'historica')
        outra_historica_mesma_data = self._semana(
            'manha', self.historica_manha_antiga.data_inicio, 'historica',
        )

        for candidata in (historica_mesma_data_ativa, outra_historica_mesma_data):
            response = self.client.post(
                reverse('editar_semana_cronograma_laboratorio'), {'semana_id': candidata.id},
                follow=True,
            )
            candidata.refresh_from_db()
            self.assertEqual(candidata.status, 'historica')
            mensagens = [str(message) for message in response.context['messages']]
            self.assertTrue(any('mesmo turno e data' in message for message in mensagens))

        self.ativa_manha.refresh_from_db()
        self.historica_manha_antiga.refresh_from_db()
        self.assertEqual(self.ativa_manha.status, 'ativa')
        self.assertEqual(self.historica_manha_antiga.status, 'historica')

    def test_get_na_acao_de_editar_semana_nao_altera_historica(self):
        response = self.client.get(
            reverse('editar_semana_cronograma_laboratorio'), {'semana_id': self.historica_manha.id},
        )

        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))
        self.historica_manha.refresh_from_db()
        self.assertEqual(self.historica_manha.status, 'historica')

    def test_ativa_e_editando_nao_podem_usar_acao_de_voltar_historica_para_edicao(self):
        for semana, status in ((self.ativa_manha, 'ativa'), (self.editando_manha, 'editando')):
            self.client.post(
                reverse('editar_semana_cronograma_laboratorio'), {'semana_id': semana.id},
            )
            semana.refresh_from_db()
            self.assertEqual(semana.status, status)

    def test_promovida_desaparece_do_historico_e_aparece_em_edicao(self):
        response = self.client.post(
            reverse('editar_semana_cronograma_laboratorio'),
            {'semana_id': self.historica_manha.id},
            follow=True,
        )

        self.assertNotIn(self.historica_manha, response.context['semanas_historicas_manha'])
        self.assertIn(self.historica_manha, response.context['semanas_editando_manha'])
        html = response.content.decode()
        self.assertIn('📝 Em edição', html)
        self.assertIn('desde 24/08/2026', html)
        self.assertIn('Ativar manhã — 24/08', html)

    def test_promovida_aceita_edicao_normal_preservando_fk(self):
        self.client.post(
            reverse('editar_semana_cronograma_laboratorio'), {'semana_id': self.historica_manha.id},
        )
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})

        response = self.client.post(f'{url}?data_inicio=2026-08-24', {
            'agendamento_id': '',
            'laboratorio_id': self.laboratorio.id,
            'data': '2026-08-24',
            'horario': '3',
            'turno': 'manha',
            'professor': self.professor.id,
            'turma': self.turma_manha.id,
            'disciplina': 'Nova aula na versao recuperada',
            'observacao': '',
            'data_inicio': '2026-08-24',
            'laboratorio_filtro': '',
            'scroll_y': '',
        })

        self.assertEqual(response.status_code, 302)
        novo = AgendamentoLab.objects.get(disciplina='Nova aula na versao recuperada')
        self.assertEqual(novo.semana_id, self.historica_manha.id)
        existentes = AgendamentoLab.objects.filter(
            disciplina__in=('Historica alvo', 'Historica segundo horario'),
        )
        self.assertTrue(all(item.semana_id == self.historica_manha.id for item in existentes))

    def test_promovida_pode_ser_ativada_depois_pelo_fluxo_normal(self):
        self.client.post(
            reverse('editar_semana_cronograma_laboratorio'), {'semana_id': self.historica_manha.id},
        )

        self.client.post(
            reverse('ativar_cronograma_laboratorio'), {'semana_id': self.historica_manha.id},
        )

        self.historica_manha.refresh_from_db()
        self.ativa_manha.refresh_from_db()
        self.assertEqual(self.historica_manha.status, 'ativa')
        self.assertEqual(self.ativa_manha.status, 'historica')

    def test_ativacao_direta_de_historica_troca_apenas_as_versoes_do_mesmo_turno(self):
        ativa_tarde = self._semana('tarde', date(2026, 8, 24), 'ativa')
        antes_agendamentos = list(
            AgendamentoLab.objects.order_by('id').values_list('id', 'semana_id'),
        )

        self.client.post(
            reverse('ativar_cronograma_laboratorio'), {'semana_id': self.historica_manha.id},
        )

        for semana in (self.historica_manha, self.ativa_manha, self.editando_manha,
                       self.historica_manha_antiga, self.historica_tarde, ativa_tarde):
            semana.refresh_from_db()
        self.assertEqual(self.historica_manha.status, 'ativa')
        self.assertEqual(self.ativa_manha.status, 'historica')
        self.assertEqual(self.editando_manha.status, 'editando')
        self.assertEqual(self.historica_manha_antiga.status, 'historica')
        self.assertEqual(self.historica_tarde.status, 'historica')
        self.assertEqual(ativa_tarde.status, 'ativa')
        self.assertEqual(
            list(AgendamentoLab.objects.order_by('id').values_list('id', 'semana_id')),
            antes_agendamentos,
        )
        self.assertEqual(
            SemanaCronogramaLaboratorio.objects.filter(turno='manha', status='ativa').count(), 1,
        )

    def test_remover_historica_cancela_e_preserva_agendamentos_e_fks(self):
        antes = list(AgendamentoLab.objects.order_by('id').values_list('id', 'semana_id'))

        response = self.client.post(
            reverse('excluir_semana_cronograma_laboratorio'),
            {'semana_id': self.historica_manha.id}, follow=True,
        )

        self.historica_manha.refresh_from_db()
        self.assertEqual(self.historica_manha.status, 'cancelada')
        self.assertEqual(self.historica_manha.cancelado_por, self.usuario)
        self.assertIsNotNone(self.historica_manha.cancelado_em)
        self.assertEqual(
            list(AgendamentoLab.objects.order_by('id').values_list('id', 'semana_id')), antes,
        )
        self.assertNotIn(self.historica_manha, response.context['semanas_historicas_manha'])

    def test_remover_historica_da_manha_nao_altera_tarde(self):
        estados_tarde = list(
            SemanaCronogramaLaboratorio.objects.filter(turno='tarde').order_by('id')
            .values_list('id', 'status'),
        )

        self.client.post(
            reverse('excluir_semana_cronograma_laboratorio'),
            {'semana_id': self.historica_manha.id},
        )

        self.assertEqual(
            list(SemanaCronogramaLaboratorio.objects.filter(turno='tarde').order_by('id')
                 .values_list('id', 'status')),
            estados_tarde,
        )

    def test_remover_por_pk_bloqueia_ativa_editando_e_cancelada(self):
        for semana, status in (
            (self.ativa_manha, 'ativa'),
            (self.editando_manha, 'editando'),
            (self.cancelada_tarde, 'cancelada'),
        ):
            self.client.post(
                reverse('excluir_semana_cronograma_laboratorio'), {'semana_id': semana.id},
            )
            semana.refresh_from_db()
            self.assertEqual(semana.status, status)

    def test_get_com_semana_id_nao_remove_historica(self):
        self.client.get(
            reverse('excluir_semana_cronograma_laboratorio'),
            {'semana_id': self.historica_manha.id},
        )

        self.historica_manha.refresh_from_db()
        self.assertEqual(self.historica_manha.status, 'historica')

    def test_consulta_cronograma_nao_ativa_nem_remove_historica_por_url_direta(self):
        grupo = Group.objects.get_or_create(name='Consulta Cronograma')[0]
        usuario = User.objects.create_user(username='consulta-gerencia-historico', password='senha-teste')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        self.client.post(
            reverse('ativar_cronograma_laboratorio'), {'semana_id': self.historica_manha.id},
        )
        self.client.post(
            reverse('excluir_semana_cronograma_laboratorio'), {'semana_id': self.historica_manha.id},
        )

        self.historica_manha.refresh_from_db()
        self.ativa_manha.refresh_from_db()
        self.assertEqual(self.historica_manha.status, 'historica')
        self.assertEqual(self.ativa_manha.status, 'ativa')

    def test_visualizar_abre_exatamente_a_historica_escolhida_em_somente_leitura(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        response = self.client.get(url, {'data_inicio': '2026-08-24'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Historica alvo')
        self.assertNotContains(response, 'Ativa atual')
        self.assertIn('Semana Histórica', response.context['cronograma_titulo'])
        self.assertFalse(response.context['pode_editar_grade'])
        self.assertNotContains(response, 'agendamentoModalForm')
        self.assertNotContains(response, 'fa-trash-alt')

    def test_pdf_usa_exatamente_a_historica_escolhida(self):
        response = self.client.get(reverse('cronograma_print'), {
            'turno': 'manha', 'data_inicio': '2026-08-24',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Historica alvo')
        self.assertNotContains(response, 'Ativa atual')

    def test_excel_usa_exatamente_a_historica_escolhida(self):
        response = self.client.get(reverse('exportar_cronograma_excel'), {
            'turno': 'manha', 'data_inicio': '2026-08-24',
        })
        workbook = load_workbook(BytesIO(response.content))
        valores = {
            cell.value
            for worksheet in workbook.worksheets
            for row in worksheet.iter_rows()
            for cell in row
            if cell.value
        }

        self.assertEqual(response.status_code, 200)
        self.assertIn('Historica alvo', valores)
        self.assertNotIn('Ativa atual', valores)

    def test_post_na_grade_historica_continua_bloqueado(self):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'manha'})
        response = self.client.post(f'{url}?data_inicio=2026-08-24', {
            'agendamento_id': '',
            'laboratorio_id': self.laboratorio.id,
            'data': '2026-08-24',
            'horario': '3',
            'turno': 'manha',
            'professor': self.professor.id,
            'turma': self.turma_manha.id,
            'disciplina': 'Escrita indevida no historico',
            'observacao': '',
            'data_inicio': '2026-08-24',
            'laboratorio_filtro': '',
            'scroll_y': '',
        })

        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Escrita indevida no historico').exists())

    def test_post_de_exclusao_na_historica_continua_bloqueado(self):
        agendamento = AgendamentoLab.objects.get(disciplina='Historica alvo')

        response = self.client.post(reverse('excluir_agendamento', args=[agendamento.id]))

        self.assertRedirects(response, reverse('diagnostico_cronograma_laboratorios'))
        self.assertTrue(AgendamentoLab.objects.filter(id=agendamento.id).exists())


class DatasRemovidasCanceladasDiagnosticoTests(TestCase):
    """Cobre o painel 'Datas removidas' do diagnostico: lista, somente para
    leitura, das SemanaCronogramaLaboratorio com status='cancelada' -
    separadas por turno - para o administrador entender por que aquela data
    nao pode ser recriada (ela continua reservada no banco, ver
    unica_semana_nao_historica_por_turno_data)."""

    def setUp(self):
        self.usuario = User.objects.create_user(
            username='diretivo-datas-removidas', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.usuario)
        self.laboratorio = Laboratorio.objects.create(
            nome='Laboratorio Removidas', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.professor = Professor.objects.create(nome_completo='Professor Removidas', ativo=True)
        self.turma_manha = Turma.objects.create(
            nome='5A', ano=2026, serie='5 Ano', ativa=True, turno='manha',
        )
        self.turma_tarde = Turma.objects.create(
            nome='8A', ano=2026, serie='8 Ano', ativa=True, turno='tarde',
        )

        self.cancelada_manha_com_horarios = self._semana(
            'manha', date(2026, 8, 24), 'cancelada',
            cancelado_por=self.usuario, cancelado_em=timezone.now(),
        )
        self.cancelada_manha_sem_horarios = self._semana('manha', date(2026, 8, 25), 'cancelada')
        self.cancelada_tarde_com_horarios = self._semana(
            'tarde', date(2026, 8, 24), 'cancelada',
            cancelado_por=self.usuario, cancelado_em=timezone.now(),
        )
        self.ativa_manha = self._semana('manha', date(2026, 8, 17), 'ativa')
        self.editando_manha = self._semana('manha', date(2026, 8, 31), 'editando')
        self.historica_manha = self._semana('manha', date(2026, 8, 3), 'historica')

        self._agendar(self.cancelada_manha_com_horarios, '1', 'Cancelada manha 1')
        self._agendar(self.cancelada_manha_com_horarios, '2', 'Cancelada manha 2')
        self._agendar(self.cancelada_tarde_com_horarios, '1', 'Cancelada tarde 1')
        self._agendar(self.ativa_manha, '1', 'Ativa nao deve aparecer')
        self._agendar(self.editando_manha, '1', 'Editando nao deve aparecer')
        self._agendar(self.historica_manha, '1', 'Historica nao deve aparecer')

    def _semana(self, turno, data_inicio, status, cancelado_por=None, cancelado_em=None):
        return SemanaCronogramaLaboratorio.objects.create(
            turno=turno, data_inicio=data_inicio, status=status,
            cancelado_por=cancelado_por, cancelado_em=cancelado_em,
        )

    def _agendar(self, semana, horario, disciplina):
        return AgendamentoLab.objects.create(
            laboratorio=self.laboratorio,
            data=semana.data_inicio,
            horario=horario,
            turno=semana.turno,
            professor=self.professor,
            turma=self.turma_manha if semana.turno == 'manha' else self.turma_tarde,
            disciplina=disciplina,
            registrado_por=self.usuario,
            semana=semana,
        )

    def _diagnostico(self):
        return self.client.get(reverse('diagnostico_cronograma_laboratorios'))

    def _html_secao(self, response, turno):
        html = response.content.decode()
        inicio = html.index(f'data-canceladas-turno="{turno}"')
        outro_turno = 'tarde' if turno == 'manha' else None
        if outro_turno:
            fim = html.index(f'data-canceladas-turno="{outro_turno}"', inicio)
            return html[inicio:fim]
        return html[inicio:html.index('</ul>', inicio)]

    def test_botao_datas_removidas_aparece_no_diagnostico(self):
        response = self._diagnostico()
        self.assertContains(response, '🗃️ Datas removidas')
        self.assertContains(response, 'id="modalDatasRemovidas"')

    def test_canceladas_da_manha_aparecem(self):
        response = self._diagnostico()
        ids_manha = {s.id for s in response.context['semanas_canceladas_manha']}
        self.assertIn(self.cancelada_manha_com_horarios.id, ids_manha)
        self.assertIn(self.cancelada_manha_sem_horarios.id, ids_manha)
        secao = self._html_secao(response, 'manha')
        self.assertIn('24/08/2026', secao)
        self.assertIn('25/08/2026', secao)

    def test_canceladas_da_tarde_aparecem(self):
        response = self._diagnostico()
        ids_tarde = {s.id for s in response.context['semanas_canceladas_tarde']}
        self.assertIn(self.cancelada_tarde_com_horarios.id, ids_tarde)
        secao = self._html_secao(response, 'tarde')
        self.assertIn('24/08/2026', secao)

    def test_ativa_editando_e_historica_nao_aparecem_na_lista_de_canceladas(self):
        response = self._diagnostico()
        ids_canceladas = {
            s.id
            for s in (
                *response.context['semanas_canceladas_manha'],
                *response.context['semanas_canceladas_tarde'],
            )
        }
        self.assertNotIn(self.ativa_manha.id, ids_canceladas)
        self.assertNotIn(self.editando_manha.id, ids_canceladas)
        self.assertNotIn(self.historica_manha.id, ids_canceladas)

    def test_quantidade_de_horarios_esta_correta(self):
        response = self._diagnostico()
        por_id = {s.id: s.horarios_total for s in response.context['semanas_canceladas_manha']}
        self.assertEqual(por_id[self.cancelada_manha_com_horarios.id], 2)
        self.assertEqual(por_id[self.cancelada_manha_sem_horarios.id], 0)
        por_id_tarde = {s.id: s.horarios_total for s in response.context['semanas_canceladas_tarde']}
        self.assertEqual(por_id_tarde[self.cancelada_tarde_com_horarios.id], 1)

    def test_versao_cancelada_com_zero_horarios_tambem_aparece(self):
        response = self._diagnostico()
        secao = self._html_secao(response, 'manha')
        self.assertIn('25/08/2026', secao)
        self.assertIn('0 horários', secao)

    def test_mostra_data_do_cancelamento_e_usuario_quando_existir(self):
        response = self._diagnostico()
        secao = self._html_secao(response, 'manha')
        self.assertIn('Cancelada em', secao)
        self.assertIn(self.usuario.username, secao)

    def test_mostra_aviso_quando_cancelamento_nao_foi_registrado(self):
        response = self._diagnostico()
        secao = self._html_secao(response, 'manha')
        self.assertIn('Data do cancelamento não registrada', secao)
        self.assertIn('usuário não registrado', secao)

    def test_nenhuma_acao_de_escrita_e_oferecida_no_painel(self):
        response = self._diagnostico()
        secao_manha = self._html_secao(response, 'manha')
        secao_tarde = self._html_secao(response, 'tarde')

        for secao in (secao_manha, secao_tarde):
            self.assertNotIn('<form', secao)
            self.assertNotIn('<button type="submit"', secao)
            self.assertNotIn('✏️ Editar', secao)
            self.assertNotIn('▶️ Ativar', secao)
            self.assertNotIn('🗑️ Remover', secao)
            self.assertNotIn('Reativar', secao)
            self.assertNotIn('Restaurar', secao)
            self.assertNotIn('Excluir', secao)

    def test_get_do_diagnostico_nao_altera_banco(self):
        antes_semanas = list(
            SemanaCronogramaLaboratorio.objects.order_by('id').values_list(
                'id', 'status', 'cancelado_por_id', 'cancelado_em',
            )
        )
        antes_agendamentos = list(
            AgendamentoLab.objects.order_by('id').values_list('id', 'semana_id'),
        )

        for _ in range(2):
            response = self._diagnostico()
            self.assertEqual(response.status_code, 200)

        depois_semanas = list(
            SemanaCronogramaLaboratorio.objects.order_by('id').values_list(
                'id', 'status', 'cancelado_por_id', 'cancelado_em',
            )
        )
        depois_agendamentos = list(
            AgendamentoLab.objects.order_by('id').values_list('id', 'semana_id'),
        )
        self.assertEqual(depois_semanas, antes_semanas)
        self.assertEqual(depois_agendamentos, antes_agendamentos)

    def test_consulta_cronograma_nao_acessa_o_painel(self):
        grupo = Group.objects.create(name='Consulta Cronograma')
        usuario = User.objects.create_user(username='consulta-datas-removidas', password='senha-teste')
        usuario.groups.add(grupo)
        self.client.force_login(usuario)

        response = self._diagnostico()

        self.assertNotEqual(response.status_code, 200)


class ConflitoAgendamentoIsoladoPorVersaoTests(TestCase):
    """Cobre a correcao do bug de producao: a checagem de conflito de
    laboratorio/professor num POST na grade (cronograma_semanal_turno), num
    turno que ja adotou SemanaCronogramaLaboratorio, deve ser restrita a
    versao aberta (semana_destino) recebida pelo POST. Antes da correcao,
    conflito_professor/conflito_lab filtravam so por laboratorio ou
    professor + data + horario + turno, ignorando a semana - um
    AgendamentoLab de QUALQUER outra versao do mesmo turno (ativa, editando,
    historica ou cancelada) com a mesma data/horario bloqueava indevidamente
    o cadastro na versao em edicao (ex.: 'Lab. 01 ja esta reservado neste
    horario', mesmo a versao aberta nao tendo nenhum agendamento la)."""

    def setUp(self):
        self.usuario = User.objects.create_user(
            username='diretivo-conflito-versao', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.usuario)
        self.lab1 = Laboratorio.objects.create(
            nome='Lab 01', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.lab2 = Laboratorio.objects.create(
            nome='Lab 02', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.professor_a = Professor.objects.create(nome_completo='Ana Paula Ferreira', ativo=True)
        self.professor_b = Professor.objects.create(nome_completo='Bruno Castro Lima', ativo=True)
        self.turma_manha = Turma.objects.create(nome='1A', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.turma_tarde = Turma.objects.create(nome='6A', ano=2026, serie='6 Ano', ativa=True, turno='tarde')

        # Versao aberta (a que recebe o POST em quase todos os testes),
        # cobrindo 22/08 a 28/08.
        self.semana_editando_tarde = self._semana('tarde', date(2026, 8, 22), 'editando')
        # Versao aberta equivalente da manha, mesma janela, para o teste de
        # independencia entre turnos.
        self.semana_editando_manha = self._semana('manha', date(2026, 8, 22), 'editando')

        # Outras versoes do MESMO turno (tarde), cada uma com status
        # diferente, que nao devem contaminar a versao em edicao mesmo
        # tendo um AgendamentoLab na mesma data/horario/turno.
        self.semana_outra_ativa = self._semana('tarde', date(2026, 8, 1), 'ativa')
        self.semana_outra_historica = self._semana('tarde', date(2026, 7, 1), 'historica')
        self.semana_outra_cancelada = self._semana('tarde', date(2026, 6, 1), 'cancelada')

    def _semana(self, turno, data_inicio, status):
        return SemanaCronogramaLaboratorio.objects.create(
            turno=turno, data_inicio=data_inicio, status=status,
        )

    def _turma(self, turno):
        return self.turma_manha if turno == 'manha' else self.turma_tarde

    def _agendar(self, semana, laboratorio, professor, horario, data, disciplina):
        """Cria um AgendamentoLab diretamente por ORM, vinculado a uma
        semana explicita - inclusive com uma data fora da janela real
        daquela semana quando o teste precisar, ja que essa checagem de
        janela e feita so no POST da grade, nunca em AgendamentoLab.save()."""
        return AgendamentoLab.objects.create(
            laboratorio=laboratorio,
            data=data,
            horario=horario,
            turno=semana.turno,
            professor=professor,
            turma=self._turma(semana.turno),
            disciplina=disciplina,
            registrado_por=self.usuario,
            semana=semana,
        )

    def _post(self, semana_destino, laboratorio, professor, horario, data, disciplina, agendamento_id=''):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': semana_destino.turno})
        return self.client.post(f'{url}?data_inicio={semana_destino.data_inicio.isoformat()}', {
            'agendamento_id': agendamento_id,
            'laboratorio_id': laboratorio.id,
            'data': data.isoformat(),
            'horario': horario,
            'turno': semana_destino.turno,
            'professor': professor.id,
            'turma': self._turma(semana_destino.turno).id,
            'disciplina': disciplina,
            'observacao': '',
            'data_inicio': semana_destino.data_inicio.isoformat(),
            'laboratorio_filtro': '',
            'scroll_y': '',
        }, follow=True)

    def test_mesmo_laboratorio_data_horario_em_versoes_diferentes_e_permitido(self):
        self._agendar(
            self.semana_outra_ativa, self.lab1, self.professor_b, '2', date(2026, 8, 24),
            disciplina='Lab na versao ativa',
        )

        response = self._post(
            self.semana_editando_tarde, self.lab1, self.professor_a, '2', date(2026, 8, 24),
            disciplina='Lab na versao editando',
        )

        mensagens = [str(mensagem) for mensagem in response.context['messages']]
        self.assertTrue(any('salvo com sucesso' in mensagem for mensagem in mensagens))
        novo = AgendamentoLab.objects.get(disciplina='Lab na versao editando')
        self.assertEqual(novo.semana_id, self.semana_editando_tarde.id)
        self.assertTrue(
            AgendamentoLab.objects.filter(
                disciplina='Lab na versao ativa', semana=self.semana_outra_ativa,
            ).exists()
        )

    def test_mesmo_professor_data_horario_em_versoes_diferentes_e_permitido(self):
        self._agendar(
            self.semana_outra_historica, self.lab2, self.professor_a, '3', date(2026, 8, 24),
            disciplina='Professor na versao historica',
        )

        response = self._post(
            self.semana_editando_tarde, self.lab1, self.professor_a, '3', date(2026, 8, 24),
            disciplina='Professor na versao editando',
        )

        mensagens = [str(mensagem) for mensagem in response.context['messages']]
        self.assertTrue(any('salvo com sucesso' in mensagem for mensagem in mensagens))
        novo = AgendamentoLab.objects.get(disciplina='Professor na versao editando')
        self.assertEqual(novo.semana_id, self.semana_editando_tarde.id)
        self.assertTrue(
            AgendamentoLab.objects.filter(
                disciplina='Professor na versao historica', semana=self.semana_outra_historica,
            ).exists()
        )

    def test_mesmo_laboratorio_data_horario_na_mesma_versao_e_bloqueado(self):
        self._agendar(
            self.semana_editando_tarde, self.lab1, self.professor_b, '4', date(2026, 8, 25),
            disciplina='Ja cadastrado na editando',
        )

        response = self._post(
            self.semana_editando_tarde, self.lab1, self.professor_a, '4', date(2026, 8, 25),
            disciplina='Tentativa de lab duplicado',
        )

        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Tentativa de lab duplicado').exists())
        mensagens = [str(mensagem) for mensagem in response.context['messages']]
        self.assertTrue(any('ja esta reservado' in mensagem for mensagem in mensagens))

    def test_mesmo_professor_data_horario_na_mesma_versao_e_bloqueado(self):
        self._agendar(
            self.semana_editando_tarde, self.lab2, self.professor_a, '5', date(2026, 8, 26),
            disciplina='Professor ja cadastrado na editando',
        )

        response = self._post(
            self.semana_editando_tarde, self.lab1, self.professor_a, '5', date(2026, 8, 26),
            disciplina='Tentativa de professor duplicado',
        )

        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Tentativa de professor duplicado').exists())
        mensagens = [str(mensagem) for mensagem in response.context['messages']]
        self.assertTrue(any('ja possui agendamento' in mensagem for mensagem in mensagens))

    def test_editar_o_proprio_agendamento_continua_permitido(self):
        existente = self._agendar(
            self.semana_editando_tarde, self.lab1, self.professor_a, '6', date(2026, 8, 27),
            disciplina='Original',
        )

        response = self._post(
            self.semana_editando_tarde, self.lab1, self.professor_a, '6', date(2026, 8, 27),
            disciplina='Original editado', agendamento_id=existente.id,
        )

        mensagens = [str(mensagem) for mensagem in response.context['messages']]
        self.assertTrue(any('salvo com sucesso' in mensagem for mensagem in mensagens))
        existente.refresh_from_db()
        self.assertEqual(existente.disciplina, 'Original editado')
        self.assertEqual(AgendamentoLab.objects.filter(laboratorio=self.lab1, horario='6').count(), 1)

    def _versao_outra_nao_contamina(self, semana_outra, rotulo, horario):
        # Cada rotulo usa seu proprio horario: as 3 semanas "outra" nao podem
        # compartilhar (lab1, mesma data, mesmo horario, tarde) entre si no
        # mesmo teste - isso colidiria no unique_together de AgendamentoLab
        # (laboratorio, data, horario, turno), que nao inclui semana e por
        # isso so aceita 1 linha por combinacao, mesmo entre 2 semanas
        # "outra" que nunca vão concorrer entre si na tela.
        self._agendar(
            semana_outra, self.lab1, self.professor_b, horario, date(2026, 8, 23),
            disciplina=f'Conflito da versao {rotulo}',
        )

        response = self._post(
            self.semana_editando_tarde, self.lab1, self.professor_a, horario, date(2026, 8, 23),
            disciplina=f'Aula real na editando ({rotulo})',
        )

        mensagens = [str(mensagem) for mensagem in response.context['messages']]
        self.assertTrue(any('salvo com sucesso' in mensagem for mensagem in mensagens))
        novo = AgendamentoLab.objects.get(disciplina=f'Aula real na editando ({rotulo})')
        self.assertEqual(novo.semana_id, self.semana_editando_tarde.id)

    def test_versao_ativa_nao_contamina_a_editando(self):
        self._versao_outra_nao_contamina(self.semana_outra_ativa, 'ativa', '1')

    def test_versao_historica_nao_contamina_a_editando(self):
        self._versao_outra_nao_contamina(self.semana_outra_historica, 'historica', '2')

    def test_versao_cancelada_nao_contamina_a_editando(self):
        self._versao_outra_nao_contamina(self.semana_outra_cancelada, 'cancelada', '3')

    def test_manha_e_tarde_sao_independentes(self):
        self._agendar(
            self.semana_editando_tarde, self.lab1, self.professor_b, '1', date(2026, 8, 22),
            disciplina='Tarde ocupa o horario',
        )

        response = self._post(
            self.semana_editando_manha, self.lab1, self.professor_a, '1', date(2026, 8, 22),
            disciplina='Manha livre no mesmo horario',
        )

        mensagens = [str(mensagem) for mensagem in response.context['messages']]
        self.assertTrue(any('salvo com sucesso' in mensagem for mensagem in mensagens))
        novo = AgendamentoLab.objects.get(disciplina='Manha livre no mesmo horario')
        self.assertEqual(novo.turno, 'manha')
        self.assertEqual(novo.semana_id, self.semana_editando_manha.id)

    def test_fk_semana_id_correta_no_novo_agendamento(self):
        response = self._post(
            self.semana_editando_tarde, self.lab1, self.professor_a, '2', date(2026, 8, 23),
            disciplina='Confere FK da semana',
        )

        mensagens = [str(mensagem) for mensagem in response.context['messages']]
        self.assertTrue(any('salvo com sucesso' in mensagem for mensagem in mensagens))
        novo = AgendamentoLab.objects.get(disciplina='Confere FK da semana')
        self.assertEqual(novo.semana_id, self.semana_editando_tarde.id)
        self.assertEqual(novo.semana.status, 'editando')
        self.assertEqual(novo.semana.turno, 'tarde')


class ConstraintUnicidadeAgendamentoLabPorVersaoTests(TestCase):
    """Testa direto no ORM (sem passar pela view) as duas UniqueConstraint
    condicionais que substituiram o antigo unique_together de AgendamentoLab
    (migration 0033_ajusta_unicidade_agendamentolab_por_versao): confirma
    que o proprio banco - nao so o filtro de conflito em Python - entende o
    conceito de versao para linhas com semana, e que linhas legadas
    (semana=NULL, de turnos que nunca adotaram SemanaCronogramaLaboratorio)
    continuam com a regra de unicidade global antiga entre si."""

    def setUp(self):
        self.usuario = User.objects.create_user(
            username='diretivo-constraint-unicidade', password='senha-teste', is_superuser=True,
        )
        self.laboratorio = Laboratorio.objects.create(
            nome='Lab Constraint', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.professor = Professor.objects.create(nome_completo='Diego Martins Souza', ativo=True)
        self.turma = Turma.objects.create(nome='8A', ano=2026, serie='8 Ano', ativa=True, turno='tarde')
        self.semana_a = SemanaCronogramaLaboratorio.objects.create(
            turno='tarde', data_inicio=date(2026, 8, 22), status='editando',
        )
        self.semana_b = SemanaCronogramaLaboratorio.objects.create(
            turno='tarde', data_inicio=date(2026, 8, 1), status='historica',
        )

    def _criar(self, semana, horario='1', data=date(2026, 8, 24), disciplina='Aula'):
        return AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=data, horario=horario, turno='tarde',
            professor=self.professor, turma=self.turma, disciplina=disciplina,
            registrado_por=self.usuario, semana=semana,
        )

    def test_mesmo_slot_em_semanas_diferentes_e_permitido_pelo_banco(self):
        self._criar(self.semana_a, disciplina='Versao A')
        self._criar(self.semana_b, disciplina='Versao B')

        self.assertEqual(AgendamentoLab.objects.filter(laboratorio=self.laboratorio, horario='1').count(), 2)

    def test_mesmo_slot_na_mesma_semana_e_bloqueado_pelo_banco(self):
        self._criar(self.semana_a, disciplina='Original')

        with self.assertRaises(IntegrityError):
            self._criar(self.semana_a, disciplina='Duplicado')

    def test_registros_legados_sem_semana_continuam_bloqueando_duplicidade(self):
        AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=date(2026, 8, 24), horario='2', turno='tarde',
            professor=self.professor, turma=self.turma, disciplina='Legado original',
            registrado_por=self.usuario, semana=None,
        )

        with self.assertRaises(IntegrityError):
            AgendamentoLab.objects.create(
                laboratorio=self.laboratorio, data=date(2026, 8, 24), horario='2', turno='tarde',
                professor=self.professor, turma=self.turma, disciplina='Legado duplicado',
                registrado_por=self.usuario, semana=None,
            )


class AlinhamentoDiasGradeCronogramaTests(TestCase):
    """Cobre a correcao do bug de producao: dias_cronograma (a lista de
    colunas de dia da grade) era montada zipando DIAS_SEMANA_LAB (nomes fixos
    'Segunda-feira'..'Sexta-feira', por posicao) com data_inicio + timedelta,
    assumindo implicitamente que toda versao comeca numa segunda-feira. Fase
    2 permite data_inicio em qualquer dia (ex.: 22/08/2026 e um sabado) - a
    grade continuava rotulando a 1a coluna como 'Segunda-feira' mesmo quando
    a data real dali era um sabado, enquanto _agendamentos_por_chave sempre
    classificou o agendamento pelo dia real (strftime('%A')). Isso descasava
    visualizacao (rotulo por posicao) e gravacao (chave pelo dia real): um
    agendamento em 22/08 nunca aparecia na celula onde foi salvo. A correcao
    (_dias_cronograma em views/laboratorios.py) deriva o nome de CADA coluna
    da data real daquela posicao, com o mesmo mapeamento usado na gravacao."""

    def setUp(self):
        self.usuario = User.objects.create_user(
            username='diretivo-alinhamento-dias', password='senha-teste', is_superuser=True,
        )
        self.client.force_login(self.usuario)
        self.laboratorio = Laboratorio.objects.create(
            nome='Lab Dias', tipo='fixo', equipamento='Computadores', ativo=True,
        )
        self.professor_a = Professor.objects.create(nome_completo='Elisa Prado Cavalcante', ativo=True)
        self.professor_b = Professor.objects.create(nome_completo='Fabio Rezende Nogueira', ativo=True)
        self.turma_tarde = Turma.objects.create(nome='9B', ano=2026, serie='9 Ano', ativa=True, turno='tarde')
        self.turma_manha = Turma.objects.create(nome='2B', ano=2026, serie='2 Ano', ativa=True, turno='manha')

    def _semana(self, turno, data_inicio, status='editando'):
        return SemanaCronogramaLaboratorio.objects.create(turno=turno, data_inicio=data_inicio, status=status)

    def _turma(self, turno):
        return self.turma_manha if turno == 'manha' else self.turma_tarde

    def _agendar(self, semana, horario, data, professor=None, disciplina='Aula'):
        return AgendamentoLab.objects.create(
            laboratorio=self.laboratorio, data=data, horario=horario, turno=semana.turno,
            professor=professor or self.professor_a, turma=self._turma(semana.turno),
            disciplina=disciplina, registrado_por=self.usuario, semana=semana,
        )

    def _grade(self, semana):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': semana.turno})
        return self.client.get(url, {'data_inicio': semana.data_inicio.isoformat()})

    def _post(self, semana, professor, horario, data, disciplina, agendamento_id=''):
        url = reverse('cronograma_semanal_turno', kwargs={'turno_forcado': semana.turno})
        return self.client.post(f'{url}?data_inicio={semana.data_inicio.isoformat()}', {
            'agendamento_id': agendamento_id,
            'laboratorio_id': self.laboratorio.id,
            'data': data.isoformat(),
            'horario': horario,
            'turno': semana.turno,
            'professor': professor.id,
            'turma': self._turma(semana.turno).id,
            'disciplina': disciplina,
            'observacao': '',
            'data_inicio': semana.data_inicio.isoformat(),
            'laboratorio_filtro': '',
            'scroll_y': '',
        }, follow=True)

    def _celula(self, response, turno, horario, dia_nome):
        for grupo in response.context['cronograma_grupos']:
            if grupo['turno'] != turno:
                continue
            for lab_cronograma in grupo['laboratorios']:
                if lab_cronograma['laboratorio'].id != self.laboratorio.id:
                    continue
                for linha in lab_cronograma['linhas']:
                    if linha['slot']['horario'] != horario:
                        continue
                    for celula in linha['celulas']:
                        if celula['dia']['nome'] == dia_nome:
                            return celula
        return None

    def _assert_ordem_visual(self, response, datas_esperadas):
        self.assertEqual(
            [(dia['nome'], dia['data']) for dia in response.context['dias_cronograma']],
            list(zip(
                ['Segunda-feira', 'Terca-feira', 'Quarta-feira', 'Quinta-feira', 'Sexta-feira'],
                datas_esperadas,
            )),
        )

    def test_versao_iniciando_numa_segunda_feira_continua_correta(self):
        semana = self._semana('tarde', date(2026, 8, 24))  # segunda-feira

        response = self._grade(semana)

        self._assert_ordem_visual(
            response,
            [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26),
             date(2026, 8, 27), date(2026, 8, 28)],
        )

    def test_versao_iniciando_numa_quarta_feira_mantem_ordem_visual_escolar(self):
        semana = self._semana('tarde', date(2026, 8, 26))  # quarta-feira

        response = self._grade(semana)

        self._assert_ordem_visual(
            response,
            [date(2026, 8, 31), date(2026, 9, 1), date(2026, 8, 26),
             date(2026, 8, 27), date(2026, 8, 28)],
        )
        self.assertEqual(response.context['data_inicio'], date(2026, 8, 26))
        semana.refresh_from_db()
        self.assertEqual(semana.data_inicio, date(2026, 8, 26))

    def test_versao_iniciando_numa_sexta_feira_mantem_ordem_visual_escolar(self):
        semana = self._semana('tarde', date(2026, 8, 28))  # sexta-feira

        response = self._grade(semana)

        self._assert_ordem_visual(
            response,
            [date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2),
             date(2026, 9, 3), date(2026, 8, 28)],
        )
        self.assertEqual(response.context['data_inicio'], date(2026, 8, 28))
        semana.refresh_from_db()
        self.assertEqual(semana.data_inicio, date(2026, 8, 28))

    def test_versao_iniciando_num_sabado_a_grade_comeca_na_segunda_seguinte(self):
        semana = self._semana('tarde', date(2026, 8, 22))  # sabado

        response = self._grade(semana)

        self._assert_ordem_visual(
            response,
            [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26),
             date(2026, 8, 27), date(2026, 8, 28)],
        )
        # a versao continua identificada pela data original (22/08, sabado),
        # so a APRESENTACAO da grade pula direto pra segunda seguinte.
        self.assertEqual(response.context['data_inicio'], date(2026, 8, 22))
        self.assertIn('22/08/2026', response.context['cronograma_titulo'])
        semana.refresh_from_db()
        self.assertEqual(semana.data_inicio, date(2026, 8, 22))

    def test_versao_iniciando_num_domingo_a_grade_comeca_na_segunda_seguinte(self):
        semana = self._semana('tarde', date(2026, 8, 23))  # domingo

        response = self._grade(semana)

        self._assert_ordem_visual(
            response,
            [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26),
             date(2026, 8, 27), date(2026, 8, 28)],
        )
        self.assertEqual(response.context['data_inicio'], date(2026, 8, 23))
        self.assertIn('23/08/2026', response.context['cronograma_titulo'])
        semana.refresh_from_db()
        self.assertEqual(semana.data_inicio, date(2026, 8, 23))

    def test_sabado_e_domingo_nunca_aparecem_na_grade(self):
        for data_inicio in (date(2026, 8, 22), date(2026, 8, 23), date(2026, 8, 24)):
            semana = self._semana('tarde', data_inicio, status='cancelada')
            response = self._grade(semana)
            nomes = [d['nome'] for d in response.context['dias_cronograma']]
            self.assertNotIn('Sabado', nomes)
            self.assertNotIn('Domingo', nomes)

    def test_agendamento_em_sabado_nao_aparece_na_grade_mas_continua_no_banco(self):
        semana = self._semana('tarde', date(2026, 8, 22))
        agendamento = self._agendar(semana, '1', date(2026, 8, 22), disciplina='Aula de sabado')

        response = self._grade(semana)

        nomes = [d['nome'] for d in response.context['dias_cronograma']]
        self.assertNotIn('Sabado', nomes)

        # nao apareceu em NENHUMA celula visivel da grade (nao vazou pra
        # segunda so por ser a primeira coluna exibida). O agendamento ainda
        # pode aparecer noutras secoes da pagina (ex.: lista de cards), que
        # nao sao a grade e nao fazem parte deste ajuste.
        for dia_nome in nomes:
            celula = self._celula(response, 'tarde', '1', dia_nome)
            self.assertIsNone(celula['agendamento'])

        # o registro em si continua intocado no banco.
        agendamento.refresh_from_db()
        self.assertEqual(agendamento.data, date(2026, 8, 22))
        self.assertEqual(agendamento.semana_id, semana.id)

    def test_celula_ocupada_nao_aparece_como_vazia(self):
        semana = self._semana('tarde', date(2026, 8, 26))  # quarta-feira
        self._agendar(semana, '2', date(2026, 8, 31), disciplina='Aula na segunda reordenada')

        response = self._grade(semana)

        celula = self._celula(response, 'tarde', '2', 'Segunda-feira')
        self.assertIsNotNone(celula['agendamento'])
        self.assertEqual(celula['dia']['data'], date(2026, 8, 31))
        self.assertEqual(celula['agendamento'].disciplina, 'Aula na segunda reordenada')
        self.assertContains(response, 'Aula na segunda reordenada')

    def test_cadastrar_em_celula_vazia_funciona(self):
        semana = self._semana('tarde', date(2026, 8, 22))

        response = self._post(
            semana, self.professor_a, '3', date(2026, 8, 26), disciplina='Nova aula quarta',
        )

        mensagens = [str(mensagem) for mensagem in response.context['messages']]
        self.assertTrue(any('salvo com sucesso' in mensagem for mensagem in mensagens))
        novo = AgendamentoLab.objects.get(disciplina='Nova aula quarta')
        self.assertEqual(novo.data, date(2026, 8, 26))
        self.assertEqual(novo.semana_id, semana.id)

    def test_conflito_ocorre_apenas_na_mesma_celula_da_mesma_versao(self):
        semana = self._semana('tarde', date(2026, 8, 22))
        self._agendar(semana, '4', date(2026, 8, 24), professor=self.professor_b, disciplina='Ja ocupada')

        response_bloqueado = self._post(
            semana, self.professor_a, '4', date(2026, 8, 24), disciplina='Tentativa na mesma celula',
        )
        self.assertFalse(AgendamentoLab.objects.filter(disciplina='Tentativa na mesma celula').exists())
        mensagens_bloqueio = [str(mensagem) for mensagem in response_bloqueado.context['messages']]
        self.assertTrue(any('ja esta reservado' in mensagem for mensagem in mensagens_bloqueio))

        response_permitido = self._post(
            semana, self.professor_a, '5', date(2026, 8, 24), disciplina='Celula diferente',
        )
        mensagens_ok = [str(mensagem) for mensagem in response_permitido.context['messages']]
        self.assertTrue(any('salvo com sucesso' in mensagem for mensagem in mensagens_ok))

    def test_versoes_sobrepostas_continuam_isoladas_por_semana_id(self):
        semana_a = self._semana('tarde', date(2026, 8, 22))  # janela 22/08-28/08
        semana_b = self._semana('tarde', date(2026, 8, 28), status='historica')  # janela 28/08-03/09

        agendamento_a = self._agendar(semana_a, '1', date(2026, 8, 28), disciplina='Versao A na sexta')
        agendamento_b = self._agendar(semana_b, '1', date(2026, 8, 28), disciplina='Versao B na sexta')

        response_a = self._grade(semana_a)
        dias_a = response_a.context['dias_cronograma']
        self.assertEqual(dias_a[-1]['nome'], 'Sexta-feira')
        self.assertEqual(dias_a[-1]['data'], date(2026, 8, 28))
        celula_a = self._celula(response_a, 'tarde', '1', 'Sexta-feira')
        self.assertEqual(celula_a['agendamento'].id, agendamento_a.id)

        response_b = self.client.get(
            reverse('cronograma_semanal_turno', kwargs={'turno_forcado': 'tarde'}),
            {'data_inicio': semana_b.data_inicio.isoformat()},
        )
        dias_b = response_b.context['dias_cronograma']
        self.assertEqual(dias_b[-1]['nome'], 'Sexta-feira')
        self.assertEqual(dias_b[-1]['data'], date(2026, 8, 28))
        celula_b = self._celula(response_b, 'tarde', '1', 'Sexta-feira')
        self.assertEqual(celula_b['agendamento'].id, agendamento_b.id)

    def test_manha_e_tarde_independentes_com_data_inicio_nao_segunda(self):
        semana_tarde = self._semana('tarde', date(2026, 8, 22))
        semana_manha = self._semana('manha', date(2026, 8, 22))
        self._agendar(semana_tarde, '1', date(2026, 8, 24), disciplina='So na tarde')  # segunda-feira

        response_manha = self._grade(semana_manha)

        celula_manha = self._celula(response_manha, 'manha', '1', 'Segunda-feira')
        self.assertIsNotNone(celula_manha)
        self.assertIsNone(celula_manha['agendamento'])
        self.assertNotContains(response_manha, 'So na tarde')

    def test_pdf_e_excel_nao_quebram_com_data_inicio_nao_segunda(self):
        semana = self._semana('tarde', date(2026, 8, 22))
        self._agendar(semana, '1', date(2026, 8, 22), disciplina='Aula para exportacao')

        response_print = self.client.get(
            reverse('cronograma_print'), {'data_inicio': semana.data_inicio.isoformat(), 'turno': 'tarde'},
        )
        self.assertEqual(response_print.status_code, 200)

        response_excel = self.client.get(
            reverse('exportar_cronograma_excel'), {'data_inicio': semana.data_inicio.isoformat(), 'turno': 'tarde'},
        )
        self.assertEqual(response_excel.status_code, 200)


class BuscaAtivaIntegracaoTests(TestCase):
    """Testes automatizados para a integração da Busca Ativa com RegistroFaltaAluno."""

    def setUp(self):
        self.grupo_equipe = Group.objects.create(name='Equipe Diretiva')
        self.grupo_relatorios = Group.objects.create(name='Relatorios')
        self.user = User.objects.create_user(username='pedagoga-busca', password='senha-teste')
        self.user.groups.add(self.grupo_equipe, self.grupo_relatorios)
        self.client.force_login(self.user)

        self.turma = Turma.objects.create(nome='3B SIST', ano=2026, serie='3 Ano', ativa=True, turno='manha')
        self.turma_outra = Turma.objects.create(nome='1A HUM', ano=2026, serie='1 Ano', ativa=True, turno='manha')
        self.aluno_1 = Aluno.objects.create(nome='Aluno Um', numero=1, turma=self.turma, ativo=True)
        self.aluno_2 = Aluno.objects.create(nome='Aluno Dois', numero=2, turma=self.turma, ativo=True)
        self.aluno_outro = Aluno.objects.create(nome='Aluno Outra Turma', numero=1, turma=self.turma_outra, ativo=True)

    def test_busca_ativa_listagem_com_registro_falta_aluno(self):
        f1 = RegistroFaltaAluno.objects.create(
            aluno=self.aluno_1,
            data=date(2026, 9, 4),
            quantidade_faltas=1,
            pedagoga='Luciana',
            registrado_por=self.user,
        )
        f2 = RegistroFaltaAluno.objects.create(
            aluno=self.aluno_2,
            data=date(2026, 9, 3),
            quantidade_faltas=1,
            pedagoga='Luciana',
            busca_ativa_realizada=True,
            registrado_por=self.user,
        )

        response = self.client.get(reverse('busca_ativa'), {'mes': 9, 'ano': 2026})
        self.assertEqual(response.status_code, 200)
        ocorrencias = list(response.context['ocorrencias'])
        self.assertIn(f1, ocorrencias)
        self.assertIn(f2, ocorrencias)

        resumo = response.context['turmas_resumo']
        self.assertIn('3B SIST', resumo)
        self.assertEqual(resumo['3B SIST']['total'], 2)
        self.assertEqual(resumo['3B SIST']['realizadas'], 1)

    def test_marcar_busca_ativa_individual_bloqueia_get(self):
        falta = RegistroFaltaAluno.objects.create(
            aluno=self.aluno_1,
            data=date(2026, 9, 4),
            quantidade_faltas=1,
            busca_ativa_realizada=False,
            registrado_por=self.user,
        )
        url = reverse('marcar_busca_ativa', args=[falta.id])
        response = self.client.get(f'{url}?mes=9&ano=2026&turma={self.turma.id}')
        self.assertEqual(response.status_code, 405)

        falta.refresh_from_db()
        self.assertFalse(falta.busca_ativa_realizada)

    def test_marcar_busca_ativa_individual_via_post(self):
        falta = RegistroFaltaAluno.objects.create(
            aluno=self.aluno_1,
            data=date(2026, 9, 4),
            quantidade_faltas=1,
            busca_ativa_realizada=False,
            registrado_por=self.user,
        )
        url = reverse('marcar_busca_ativa', args=[falta.id])
        response = self.client.post(url, {
            'mes': '9',
            'ano': '2026',
            'turma': str(self.turma.id),
        })
        self.assertRedirects(response, f'/busca-ativa/?mes=9&ano=2026&turma={self.turma.id}')

        falta.refresh_from_db()
        self.assertTrue(falta.busca_ativa_realizada)

    def test_marcar_busca_ativa_individual_exige_csrf(self):
        from django.test import Client
        falta = RegistroFaltaAluno.objects.create(
            aluno=self.aluno_1,
            data=date(2026, 9, 4),
            quantidade_faltas=1,
            busca_ativa_realizada=False,
            registrado_por=self.user,
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        url = reverse('marcar_busca_ativa', args=[falta.id])
        response = csrf_client.post(url, {'mes': '9', 'ano': '2026'})
        self.assertEqual(response.status_code, 403)

        falta.refresh_from_db()
        self.assertFalse(falta.busca_ativa_realizada)

    def test_marcar_todos_busca_ativa(self):
        f1 = RegistroFaltaAluno.objects.create(aluno=self.aluno_1, data=date(2026, 9, 4), busca_ativa_realizada=False)
        f2 = RegistroFaltaAluno.objects.create(aluno=self.aluno_2, data=date(2026, 9, 4), busca_ativa_realizada=False)
        f_outra = RegistroFaltaAluno.objects.create(aluno=self.aluno_outro, data=date(2026, 9, 4), busca_ativa_realizada=False)

        response = self.client.post(reverse('marcar_todos_busca_ativa'), {
            'mes': '9',
            'ano': '2026',
            'turma': str(self.turma.id),
        })
        self.assertRedirects(response, reverse('busca_ativa'))

        f1.refresh_from_db()
        f2.refresh_from_db()
        f_outra.refresh_from_db()
        self.assertTrue(f1.busca_ativa_realizada)
        self.assertTrue(f2.busca_ativa_realizada)
        self.assertFalse(f_outra.busca_ativa_realizada)

    def test_editar_busca_ativa_apenas_campos_de_contato(self):
        falta = RegistroFaltaAluno.objects.create(
            aluno=self.aluno_1,
            data=date(2026, 9, 4),
            quantidade_faltas=1,
            pedagoga='Luciana',
            registrado_por=self.user,
            busca_ativa_realizada=False,
        )

        url_edit = reverse('editar_busca_ativa', args=[falta.id])
        response_get = self.client.get(f'{url_edit}?mes=9&ano=2026&turma={self.turma.id}')
        self.assertEqual(response_get.status_code, 200)

        data_post = {
            'responsavel_contatado': 'Mãe (Clara)',
            'horario_contato': '10:45',
            'alegado_responsavel': 'Aluno com febre e dor de cabeça',
            # Tentativas de alterar campos protegidos que a view não deve permitir:
            'aluno': self.aluno_2.id,
            'data': '2026-08-01',
        }
        response_post = self.client.post(f'{url_edit}?mes=9&ano=2026&turma={self.turma.id}', data_post)
        self.assertRedirects(response_post, f'/busca-ativa/?mes=9&ano=2026&turma={self.turma.id}')

        falta.refresh_from_db()
        self.assertEqual(falta.responsavel_contatado, 'Mãe (Clara)')
        self.assertEqual(falta.horario_contato, time(10, 45))
        self.assertEqual(falta.alegado_responsavel, 'Aluno com febre e dor de cabeça')
        self.assertTrue(falta.busca_ativa_realizada)
        # Garante que aluno e data permaneceram inalterados
        self.assertEqual(falta.aluno_id, self.aluno_1.id)
        self.assertEqual(falta.data, date(2026, 9, 4))

    def test_exportar_busca_ativa_excel_com_dados_reais(self):
        RegistroFaltaAluno.objects.create(
            aluno=self.aluno_1,
            data=date(2026, 9, 4),
            quantidade_faltas=1,
            pedagoga='Luciana',
            responsavel_contatado='Pai (Roberto)',
            horario_contato=time(9, 30),
            alegado_responsavel='Consulta médica comprovada',
            busca_ativa_realizada=True,
            observacoes='Ausente na 1a e 2a aula',
            registrado_por=self.user,
        )

        url = reverse('exportar_busca_ativa')
        response = self.client.get(f'{url}?mes=9&ano=2026')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

        df = pd.read_excel(BytesIO(response.content))
        self.assertEqual(len(df), 1)
        self.assertNotEqual(df.iloc[0]['Data'], 'Sem dados para o período')
        self.assertEqual(df.iloc[0]['Aluno'], 'Aluno Um')
        self.assertEqual(df.iloc[0]['Turma'], '3B SIST')
        self.assertEqual(df.iloc[0]['Busca Ativa Realizada'], 'Sim')
        self.assertEqual(df.iloc[0]['Responsável Contatado'], 'Pai (Roberto)')
        self.assertEqual(df.iloc[0]['Hora Contato'], '09:30')
        self.assertEqual(df.iloc[0]['Alegado pelo Responsável'], 'Consulta médica comprovada')
