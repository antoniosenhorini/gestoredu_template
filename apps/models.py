from datetime import timedelta

from django.db import models
from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

class Evento(models.Model):
    titulo = models.CharField(max_length=200)
    descricao = models.TextField()  # ← AGORA CAMPO SIMPLES
    data = models.DateTimeField()
    local = models.CharField(max_length=200)
    imagem = models.ImageField(upload_to='eventos/', blank=True, null=True)
    criado_por = models.ForeignKey(User, on_delete=models.CASCADE)
    criado_em = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.titulo

class Documento(models.Model):
    titulo = models.CharField(max_length=200)
    arquivo = models.FileField(upload_to='documentos/')
    categoria = models.CharField(max_length=100)
    descricao = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.titulo

class Recado(models.Model):
    titulo = models.CharField(max_length=200)
    conteudo = models.TextField()
    fixado = models.BooleanField(default=False)
    criado_por = models.ForeignKey(User, on_delete=models.CASCADE)
    criado_em = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.titulo

# ===== 🆕 NOVO MODELO PROFESSOR (COLOQUE AQUI) =====

class Professor(models.Model):
    usuario = models.OneToOneField(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Usuário (login)"
    )

    nome_completo = models.CharField(max_length=200, verbose_name="Nome completo")
    nome_social = models.CharField(max_length=200, blank=True, verbose_name="Nome social")
    data_nascimento = models.DateField(null=True, blank=True, verbose_name="Data de nascimento")
    cpf = models.CharField(max_length=14, unique=True, blank=True, null=True, verbose_name="CPF")
    rg = models.CharField(max_length=20, blank=True, verbose_name="RG")

    email = models.EmailField(blank=True, null=True, verbose_name="E-mail")  # ← OPCIONAL
    email_pessoal = models.EmailField(blank=True, verbose_name="E-mail pessoal")
    telefone = models.CharField(max_length=20, blank=True, verbose_name="Telefone")
    celular = models.CharField(max_length=20, blank=True, null=True, verbose_name="Celular")  # ← OPCIONAL

    endereco = models.TextField(blank=True, verbose_name="Endereço")
    bairro = models.CharField(max_length=100, blank=True, verbose_name="Bairro")
    cidade = models.CharField(max_length=100, blank=True, verbose_name="Cidade")
    cep = models.CharField(max_length=10, blank=True, verbose_name="CEP")

    disciplinas = models.TextField(blank=True, verbose_name="Disciplinas lecionadas")
    formacao = models.TextField(blank=True, verbose_name="Formação acadêmica")
    data_admissao = models.DateField(null=True, blank=True, verbose_name="Data de admissão")
    carga_horaria = models.IntegerField(default=40, verbose_name="Carga horária semanal")

    curriculo = models.FileField(upload_to='curriculos/', blank=True, null=True)
    documento_identidade = models.FileField(upload_to='documentos_professores/', blank=True, null=True)
    comprovante_residencia = models.FileField(upload_to='documentos_professores/', blank=True, null=True)

    ativo = models.BooleanField(default=True, verbose_name="Ativo")
    observacoes = models.TextField(blank=True, verbose_name="Observações")

    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.nome_completo

    @property
    def nome_abreviado(self):
        try:
            if not self.nome_completo:
                return ""

            partes = self.nome_completo.split()

            if len(partes) == 1:
                return partes[0]

            primeiro_nome = partes[0]

            # trata casos tipo JUNIOR, FILHO, NETO
            sufixos = ['JUNIOR', 'FILHO', 'NETO', 'SOBRINHO', 'FILHA']
            ultima = partes[-1].upper()

            if ultima in sufixos:
                iniciais = " ".join([p[0].upper() + "." for p in partes[1:-1]])
                return f"{primeiro_nome} {iniciais} {partes[-1].capitalize()}"

            iniciais = " ".join([p[0].upper() + "." for p in partes[1:]])

            return f"{primeiro_nome} {iniciais}"

        except Exception:
            return self.nome_completo

    class Meta:
        verbose_name = "Professor"
        verbose_name_plural = "Professores"
        ordering = ['nome_completo']


# ===== MODELOS DO PAINEL DA EQUIPE =====

class DocumentoPrivado(models.Model):
    titulo = models.CharField(max_length=200)
    arquivo = models.FileField(upload_to='documentos_privados/')
    categoria = models.CharField(max_length=100, blank=True, null=True)
    descricao = models.TextField(blank=True, null=True)
    criado_por = models.ForeignKey(User, on_delete=models.CASCADE)
    criado_em = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.titulo

    class Meta:
        verbose_name = "Documento Privado"
        verbose_name_plural = "Documentos Privados"
        ordering = ['-criado_em']

class EventoPrivado(models.Model):
    titulo = models.CharField(max_length=200)
    descricao = models.TextField(blank=True, null=True)
    data_inicio = models.DateTimeField()
    data_fim = models.DateTimeField(blank=True, null=True)
    local = models.CharField(max_length=200, blank=True, null=True)
    criado_por = models.ForeignKey(User, on_delete=models.CASCADE)
    criado_em = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.titulo

    class Meta:
        ordering = ['data_inicio']
        verbose_name = "Evento Privado"
        verbose_name_plural = "Eventos Privados"

class RecadoInterno(models.Model):
    mensagem = models.TextField()
    arquivo = models.FileField(upload_to='recados_internos/', blank=True, null=True)
    criado_por = models.ForeignKey(User, on_delete=models.CASCADE)
    criado_em = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.criado_por.username} - {self.criado_em.strftime('%d/%m/%Y %H:%M')}"

    class Meta:
        ordering = ['-criado_em']
        verbose_name = "Recado Interno"
        verbose_name_plural = "Recados Internos"


class AvisoProfessor(models.Model):
    TIPO_CHOICES = [
        ('ATESTADO', 'Atestado'),
        ('FALTA', 'Falta'),
        ('ATRASO', 'Atraso'),
    ]

    professor_nome = models.CharField(max_length=150)
    disciplina = models.CharField(max_length=100)
    data = models.DateField()
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    quantidade_dias_atestado = models.PositiveIntegerField(null=True, blank=True)
    horario_chegada = models.TimeField(null=True, blank=True)
    observacao = models.TextField(blank=True, null=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    visualizado = models.BooleanField(default=False)
    visualizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='avisos_professores_visualizados',
    )
    visualizado_em = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'{self.professor_nome} - {self.get_tipo_display()} em {self.data:%d/%m/%Y}'

    class Meta:
        ordering = ['-criado_em']
        verbose_name = "Aviso de Professor"
        verbose_name_plural = "Avisos de Professores"

# ===== NOVO: CONTROLE DE FALTAS =====

class PeriodoAula(models.Model):
    """Tabela fixa com os períodos do dia"""
    TURNO_CHOICES = [
        ('manha', 'Manhã'), ('integral', 'Integral'),
        ('tarde', 'Tarde'),
    ]

    turno = models.CharField(max_length=10, choices=TURNO_CHOICES)
    ordem = models.IntegerField(help_text="1º ao 12º período")
    inicio = models.TimeField()
    fim = models.TimeField()
    descricao = models.CharField(max_length=50, blank=True, help_text="Ex: Intervalo, Almoço")

    class Meta:
        ordering = ['turno', 'ordem']
        unique_together = ['turno', 'ordem']

    def __str__(self):
        return f"{self.get_turno_display()} - {self.ordem}º ({self.inicio} às {self.fim})"

class RegistroFalta(models.Model):
    TURNOS = [
        ('manha', '🌅 Manhã'),
        ('tarde', '☀️ Tarde'),
        ('integral', '📅 Integral'),
    ]

    TIPOS = [
        ('presente', '✅ Presente'),
        ('atraso', '⏰ Atraso'),
        ('falta', '❌ Falta'),
        ('justificada', '📄 Ausência Justificada'),
    ]

    professor = models.ForeignKey(User, on_delete=models.CASCADE, related_name='faltas')
    data = models.DateField()
    turno = models.CharField(max_length=10, choices=TURNOS, default='manha')  # NOVO
    dia_semana = models.CharField(max_length=20, editable=False)

    # Horários (agora livres)
    horario_previsto = models.TimeField()  # Hora que deveria chegar
    horario_real = models.TimeField(null=True, blank=True)  # Hora que chegou
    minutos_atraso = models.IntegerField(default=0, editable=False)

    tipo = models.CharField(max_length=20, choices=TIPOS, default='presente')
    observacao = models.TextField(blank=True)
    registrado_por = models.ForeignKey(User, on_delete=models.CASCADE, related_name='registros_faltas')
    registrado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-data']

    def save(self, *args, **kwargs):
        from django.utils.dateformat import format
        self.dia_semana = format(self.data, 'l')

        # Calcula atraso
        if self.tipo == 'atraso' and self.horario_real:
            previsto_min = self.horario_previsto.hour * 60 + self.horario_previsto.minute
            real_min = self.horario_real.hour * 60 + self.horario_real.minute
            self.minutos_atraso = max(0, real_min - previsto_min)
        else:
            self.minutos_atraso = 0

        super().save(*args, **kwargs)

    def minutos_faltados(self):
        if self.tipo == 'presente':
            return 0
        if self.tipo == 'atraso':
            return self.minutos_atraso
        if self.tipo in ['falta', 'justificada']:
            # Calcula com base no horário previsto (manhã ou tarde)
            if self.horario_previsto:
                hora = self.horario_previsto.hour
                if hora < 12:  # manhã
                    return 300  # 5 horas
                else:  # tarde
                    return 300  # 5 horas
            return 300  # padrão
        return 0

        # ===== VÍDEOS DO COLÉGIO =====
class Video(models.Model):
    titulo = models.CharField(max_length=200)
    arquivo = models.FileField(upload_to='videos/')
    thumbnail = models.ImageField(upload_to='videos/thumbnails/', blank=True, null=True)
    descricao = models.TextField(blank=True, null=True)
    criado_por = models.ForeignKey(User, on_delete=models.CASCADE)
    criado_em = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.titulo

    class Meta:
        ordering = ['-criado_em']
        verbose_name = "Vídeo"
        verbose_name_plural = "Vídeos"

        # ===== NOVO SISTEMA DE PONTO ELETRÔNICO =====
class RegistroPonto(models.Model):
    professor = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pontos')
    data = models.DateField()
    periodo = models.ForeignKey(PeriodoAula, on_delete=models.CASCADE)

    # Horários previstos (vem do período)
    horario_previsto_inicio = models.TimeField()
    horario_previsto_fim = models.TimeField()

    # Horários reais (registrados pela equipe)
    entrada_real = models.DateTimeField(null=True, blank=True)
    saida_real = models.DateTimeField(null=True, blank=True)

    # Controle
    observacao = models.TextField(blank=True)
    registrado_por = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pontos_registrados')
    registrado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['professor', 'data', 'periodo']
        ordering = ['-data', 'periodo__ordem']
        verbose_name = "Registro de Ponto"
        verbose_name_plural = "Registros de Ponto"

    def horas_trabalhadas(self):
        """Calcula horas efetivamente trabalhadas"""
        if self.entrada_real and self.saida_real:
            diferenca = self.saida_real - self.entrada_real
            return round(diferenca.total_seconds() / 3600, 2)
        return 0

    def horas_previstas(self):
        """Calcula horas que deveria ter trabalhado"""
        import datetime
        inicio = datetime.datetime.combine(self.data, self.horario_previsto_inicio)
        fim = datetime.datetime.combine(self.data, self.horario_previsto_fim)
        return round((fim - inicio).total_seconds() / 3600, 2)

    def saldo(self):
        """Saldo de horas (positivo = trabalhou mais, negativo = trabalhou menos)"""
        return round(self.horas_trabalhadas() - self.horas_previstas(), 2)

    def __str__(self):
        return f"{self.professor.username} - {self.data} - {self.periodo}"


        # ===== CONTROLE DE FALTAS - ALUNOS =====

class Turma(models.Model):
    TURNO_CHOICES = [
        ('manha', 'Manhã'), ('integral', 'Integral'),
        ('tarde', 'Tarde'),
    ]

    nome = models.CharField(max_length=10)  # Ex: 3A, 3B
    ano = models.IntegerField()  # 2026
    serie = models.CharField(max_length=20)  # 3º Ano
    ativa = models.BooleanField(default=True)
    turno = models.CharField(
        max_length=10,
        choices=TURNO_CHOICES,
        blank=True,
        null=True,
        verbose_name='Turno'
    )

    class Meta:
        ordering = ['nome']

    def __str__(self):
        # Pega o curso do primeiro aluno da turma
        primeiro_aluno = self.alunos.first()
        curso = f" - {primeiro_aluno.curso}" if primeiro_aluno and primeiro_aluno.curso else ""
        return f"{self.nome}{curso}"


class Aluno(models.Model):
    """Alunos da escola"""
    nome = models.CharField(max_length=100)
    numero = models.IntegerField()
    turma = models.ForeignKey(Turma, on_delete=models.CASCADE, related_name='alunos')
    ativo = models.BooleanField(default=True)
    telefone = models.CharField(max_length=20, blank=True, null=True, verbose_name="Telefone")
    curso = models.CharField(max_length=100, blank=True, null=True, verbose_name="Curso")  # <-- ADICIONE ESTA LINHA

    class Meta:
        ordering = ['turma', 'numero']
        unique_together = ['turma', 'numero']

    def __str__(self):
        return f"{self.numero} - {self.nome} ({self.turma.nome})"


class AtestadoAluno(models.Model):
    """Periodo de atestado de aluno."""
    aluno = models.ForeignKey(Aluno, on_delete=models.CASCADE, related_name='atestados')
    data_inicio = models.DateField()
    data_fim = models.DateField()
    observacao = models.TextField(blank=True)
    criado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='atestados_alunos_criados')
    criado_em = models.DateTimeField(auto_now_add=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ['-data_inicio', 'aluno__turma', 'aluno__numero']
        verbose_name = "Atestado de Aluno"
        verbose_name_plural = "Atestados de Alunos"

    def __str__(self):
        return f"{self.aluno.nome} - {self.data_inicio:%d/%m/%Y} a {self.data_fim:%d/%m/%Y}"


class RegistroFaltaAluno(models.Model):
    """Registro de faltas dos alunos"""
    aluno = models.ForeignKey(Aluno, on_delete=models.CASCADE, related_name='faltas')
    data = models.DateField()
    quantidade_faltas = models.IntegerField(default=1, help_text="Número de aulas faltadas")
    justificada = models.BooleanField(default=False)
    responsavel_contatado = models.CharField(max_length=100, blank=True, help_text="Nome do responsável contatado")
    horario_contato = models.TimeField(null=True, blank=True, verbose_name="Horário do contato")
    alegado_responsavel = models.TextField(blank=True, null=True, default='', verbose_name="O que foi alegado pelo responsável")
    busca_ativa_realizada = models.BooleanField(default=False, null=True, blank=True, verbose_name="Busca Ativa realizada")
    observacoes = models.TextField(blank=True)
    pedagoga = models.CharField(max_length=100, blank=True, default='')
    justificativa = models.TextField(blank=True, null=True)
    responsavel_justificativa = models.CharField(max_length=150, blank=True, null=True)
    data_justificativa = models.DateField(blank=True, null=True)

    # Controle
    registrado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='faltas_alunos_registradas')
    registrado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-data', 'aluno__turma', 'aluno__numero']
        verbose_name = "Falta de Aluno"
        verbose_name_plural = "Faltas de Alunos"

    def __str__(self):
        status = "Justificada" if self.justificada else "Não justificada"
        return f"{self.aluno.nome} - {self.data} - {self.quantidade_faltas} falta(s) ({status})"

       # ===== REGISTRO DE OCORRÊNCIAS DE ALUNOS =====

class RegistroOcorrenciaAluno(models.Model):
    """Registro de ocorrências/atrasos de alunos (pedagoga)"""

    TIPO_CHOICES = [
        ('falta', 'Falta'),
        ('atraso', 'Atraso'),
        ('piercing', 'Uso de Piercing'),
        ('cabelo', 'Cabelo'),
        ('uniforme', 'Uniforme'),
        ('desvio_normas', 'Desvio de Normas'),
    ]


    # NOVO: Turno choices
    TURNO_CHOICES = [
        ('manha', 'Manhã'), ('integral', 'Integral'),
        ('tarde', 'Tarde'),
    ]

    aluno = models.ForeignKey(Aluno, on_delete=models.CASCADE, related_name='ocorrencias')
    data = models.DateField(default=timezone.now)
    faltou = models.BooleanField(default=False, verbose_name="Faltou")
    tipo_ocorrencia = models.CharField(max_length=20, choices=TIPO_CHOICES, default='falta', verbose_name="Tipo de ocorrência")
    busca_ativa_realizada = models.BooleanField(default=False, verbose_name="Busca Ativa realizada")

    # NOVO CAMPO
    turno = models.CharField(max_length=10, choices=TURNO_CHOICES, default='manha', verbose_name="Turno")

    # NOVO CAMPO - Para ATA/Desvio de normas
    observacoes_adicionais = models.TextField(
        blank=True,
        null=True,
        verbose_name="Observações Adicionais (ATA)"
    )

    # Dados do atraso/ocorrência
    horario_chegada = models.TimeField(verbose_name="Horário de chegada", null=True, blank=True)
    motivo_alegado = models.TextField(verbose_name="Motivo alegado pelo aluno", blank=True)

    # Quem atendeu
    atendido_por = models.CharField(max_length=100, verbose_name="Atendido por (pedagoga)", blank=True)

    # Contato com responsável
    responsavel_contatado = models.CharField(max_length=100, verbose_name="Nome do responsável contatado", blank=True)
    horario_contato = models.TimeField(verbose_name="Horário do contato", null=True, blank=True)
    alegado_responsavel = models.TextField(verbose_name="O que foi alegado pelo responsável", blank=True)

    # NOVOS CAMPOS - Resposta via disparo (WhatsApp)
    resposta_disparo = models.TextField(
        blank=True,
        null=True,
        verbose_name="Resposta via Disparo (WhatsApp)"
    )
    resposta_disparo_data = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Data da resposta via disparo"
    )

    # Controle
    registrado_em = models.DateTimeField(auto_now_add=True)
    registrado_por = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='ocorrencias_registradas'
    )

    class Meta:
        ordering = ['-data', '-horario_chegada']
        verbose_name = "Ocorrência de Aluno"
        verbose_name_plural = "Ocorrências de Alunos"
        constraints = [
            models.UniqueConstraint(
                fields=['aluno', 'data', 'tipo_ocorrencia'],
                name='unico_registro_por_aluno_data_tipo'
            )
        ]

    def __str__(self):
        return f"{self.aluno.nome} - {self.data} {self.horario_chegada}"

    def dia_semana(self):
        from django.utils.dateformat import format
        return format(self.data, 'l')

# ===== LOG DE ACESSOS =====

class LogLogin(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    data_hora = models.DateTimeField(auto_now_add=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    def __str__(self):
        return f"{self.usuario.username} - {self.data_hora}"

    class Meta:
        ordering = ['-data_hora']
        verbose_name = "Log de Acesso"
        verbose_name_plural = "Logs de Acessos"

# =============================================================================
# GESTÃO DE LABORATÓRIOS E EMPRÉSTIMOS
# =============================================================================

class ConfiguracaoCronogramaLaboratorio(models.Model):
    # Campos antigos mantidos temporariamente para compatibilidade.
    data_inicio_cronograma_ativo = models.DateField()
    data_fim_cronograma_ativo = models.DateField()

    # Cronograma ativo separado por turno.
    data_inicio_manha_ativo = models.DateField(null=True, blank=True)
    data_fim_manha_ativo = models.DateField(null=True, blank=True)
    data_inicio_tarde_ativo = models.DateField(null=True, blank=True)
    data_fim_tarde_ativo = models.DateField(null=True, blank=True)

    # Semana em preparacao (ainda nao ativada), separada por turno e
    # compartilhada entre toda a equipe diretiva - antes ficava presa a
    # sessao de cada usuario (request.session), por isso um administrador
    # nunca via a semana que outro estava preparando. So o inicio e
    # guardado: o fim e sempre inicio + 6 dias (semana fechada de segunda a
    # domingo), a mesma regra ja usada para os campos "_ativo" acima em todo
    # o modulo - um campo "fim" aqui seria dado redundante e sempre
    # derivavel, entao nao foi criado.
    data_inicio_manha_editando = models.DateField(null=True, blank=True)
    data_inicio_tarde_editando = models.DateField(null=True, blank=True)

    atualizado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Cronograma ativo: {self.data_inicio_cronograma_ativo} a {self.data_fim_cronograma_ativo}"

    class Meta:
        ordering = ['-atualizado_em']
        verbose_name = "Configuração do Cronograma de Laboratório"
        verbose_name_plural = "Configurações do Cronograma de Laboratório"


class Laboratorio(models.Model):
    TIPO_CHOICES = [
        ('fixo', 'Fixo - Turma inteira'),
        ('itinerante', 'Itinerante - Equipamentos avulsos'),
    ]

    nome = models.CharField(max_length=10, unique=True)
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    equipamento = models.CharField(max_length=30)
    quantidade_total = models.IntegerField(default=0, help_text="Para itinerantes: 50 tablets, 30 celulares")
    descricao = models.TextField(blank=True)
    ativo = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.nome} - {self.equipamento}"


class ItemEquipamento(models.Model):
    """Controle individual de cada tablet/celular (Lab 04 e Lab 05)"""
    laboratorio = models.ForeignKey(Laboratorio, on_delete=models.CASCADE, related_name='itens')
    numero = models.IntegerField()
    patrimonio = models.CharField(max_length=50, blank=True)
    disponivel = models.BooleanField(default=True)
    observacao = models.TextField(blank=True)

    def __str__(self):
        return f"{self.laboratorio.nome} - Nº {self.numero:02d}"

    class Meta:
        unique_together = ['laboratorio', 'numero']


class AgendamentoLab(models.Model):
    """Para laboratórios fixos (Lab 01, 02, 03) - agendamento por aula"""
    HORARIO_CHOICES = [(str(i), f'{i}ª Aula') for i in range(1, 7)]
    TURNO_CHOICES = [('manha', 'Manhã'), ('integral', 'Integral'), ('tarde', 'Tarde')]

    laboratorio = models.ForeignKey(Laboratorio, on_delete=models.CASCADE, limit_choices_to={'tipo': 'fixo'})
    data = models.DateField()
    horario = models.CharField(max_length=1, choices=HORARIO_CHOICES)
    turno = models.CharField(max_length=10, choices=TURNO_CHOICES)

    professor = models.ForeignKey('Professor', on_delete=models.CASCADE)
    disciplina = models.CharField(max_length=100)
    turma = models.ForeignKey('Turma', on_delete=models.CASCADE)
    observacao = models.TextField(blank=True)

    registrado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    registrado_em = models.DateTimeField(auto_now_add=True)

    # Fase 1 da reestrutura de semanas do cronograma de laboratorios: FK
    # nullable e nao obrigatoria, apenas preparando o terreno para permitir
    # multiplas semanas em edicao por turno no futuro. Nenhum fluxo em
    # producao le ou escreve este campo ainda - so a migration de dados
    # desta fase o preenche retroativamente quando a associacao e
    # inequivoca. on_delete=PROTECT porque uma semana com agendamentos
    # vinculados nao deve poder ser apagada.
    semana = models.ForeignKey(
        'SemanaCronogramaLaboratorio',
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name='agendamentos',
    )

    class Meta:
        constraints = [
            # Fase 2: cada SemanaCronogramaLaboratorio e uma versao
            # independente do cronograma - duas versoes do mesmo turno podem
            # ter janelas de datas sobrepostas (ver _versao_ativa_turno em
            # views/laboratorios.py) e por isso podem, legitimamente, usar o
            # mesmo laboratorio+data+horario+turno cada uma na sua propria
            # versao. A unicidade so faz sentido DENTRO de uma versao.
            models.UniqueConstraint(
                fields=['semana', 'laboratorio', 'data', 'horario', 'turno'],
                condition=models.Q(semana__isnull=False),
                name='unico_slot_por_semana',
            ),
            # Registros legados (semana NULL, de turnos que ainda nao
            # adotaram SemanaCronogramaLaboratorio) nao tem versao para
            # distinguir - preserva a regra antiga de unicidade global por
            # laboratorio+data+horario+turno so para esse subconjunto.
            models.UniqueConstraint(
                fields=['laboratorio', 'data', 'horario', 'turno'],
                condition=models.Q(semana__isnull=True),
                name='unico_slot_legado_sem_semana',
            ),
        ]


class SemanaCronogramaLaboratorio(models.Model):
    """Fase 1 da reestrutura de semanas do cronograma de laboratorios.

    Estrutura paralela/preparatoria: nesta fase nada em producao le ou
    escreve nesta tabela alem da migration de dados que a povoa a partir
    do estado atual de ConfiguracaoCronogramaLaboratorio. Os campos
    antigos (data_inicio_*_ativo/_editando etc.) continuam sendo a fonte
    de verdade usada pelo sistema. Isso permite, em fases futuras,
    suportar multiplas semanas em edicao por turno mantendo so 1 semana
    ativa por turno e preservando historico.
    """

    STATUS_CHOICES = [
        ('editando', 'Em edição'),
        ('ativa', 'Ativa'),
        ('historica', 'Histórica'),
        ('cancelada', 'Cancelada'),
    ]

    turno = models.CharField(max_length=10, choices=AgendamentoLab.TURNO_CHOICES)
    data_inicio = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='editando')

    criado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    criado_em = models.DateTimeField(auto_now_add=True)

    ativado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    ativado_em = models.DateTimeField(null=True, blank=True)

    cancelado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    cancelado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-data_inicio']
        indexes = [
            models.Index(fields=['turno', 'status']),
            models.Index(fields=['turno', 'data_inicio']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['turno'],
                condition=models.Q(status='ativa'),
                name='unica_semana_ativa_por_turno',
            ),
            models.UniqueConstraint(
                fields=['turno', 'data_inicio'],
                condition=models.Q(status__in=['editando', 'ativa']),
                name='unica_semana_nao_historica_por_turno_data',
            ),
        ]

    @property
    def data_fim(self):
        return self.data_inicio + timedelta(days=6)

    @property
    def pode_receber_escrita(self):
        """Fase 2: uma semana so aceita criacao/edicao de AgendamentoLab via
        grade quando esta ATIVA ou EM EDICAO. Historica e cancelada sao
        somente leitura (consulta, PDF, Excel continuam funcionando)."""
        return self.status in ('ativa', 'editando')

    def __str__(self):
        return f"{self.get_turno_display()} - {self.data_inicio} ({self.get_status_display()})"


class Emprestimo(models.Model):
    """Para laboratórios itinerantes (Lab 04, 05) - empréstimo de equipamentos"""
    TIPO_EMPRESTIMO_CHOICES = [
        ('professor', 'Professor'),
        ('aluno', 'Aluno'),
        ('turma', 'Turma'),
    ]
    STATUS_CHOICES = [
        ('emprestado', 'Emprestado'),
        ('devolvido', 'Devolvido'),
        ('atrasado', 'Atrasado'),
    ]

    tipo_emprestimo = models.CharField(max_length=20, choices=TIPO_EMPRESTIMO_CHOICES)

    professor = models.ForeignKey('Professor', on_delete=models.SET_NULL, null=True, blank=True)
    aluno = models.ForeignKey('Aluno', on_delete=models.SET_NULL, null=True, blank=True)
    turma = models.ForeignKey('Turma', on_delete=models.SET_NULL, null=True, blank=True)

    laboratorio = models.ForeignKey(Laboratorio, on_delete=models.CASCADE, limit_choices_to={'tipo': 'itinerante'})
    quantidade = models.IntegerField()
    itens = models.ManyToManyField(ItemEquipamento, blank=True, related_name='emprestimos')

    data_emprestimo = models.DateTimeField(auto_now_add=True)
    data_prevista_devolucao = models.DateField()
    data_devolucao = models.DateTimeField(null=True, blank=True)

    motivo = models.TextField()
    aulas_utilizacao = models.CharField(max_length=50, blank=True)
    observacao = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='emprestado')

    registrado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    registrado_em = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        tomador = self.aluno or self.professor or self.turma
        return f"{self.laboratorio.nome} - {self.quantidade} itens - {tomador}"


# =============================================================================
# VERIFICACAO DO CRONOGRAMA DOS LABORATORIOS PELO PDF DE HORARIO DOS PROFESSORES
# =============================================================================

TURNO_CHOICES_LAB = [
    ('manha', 'Manha'), ('integral', 'Integral'),
    ('tarde', 'Tarde'),
    ('integral', 'Integral'), # <-- ADICIONE ESTA LINHA
]


class HorarioProfessorPDF(models.Model):
    """Registro de cada PDF de horario de professores enviado para conferencia.

    O arquivo e mantido apenas como referencia de leitura: nada aqui e usado
    para reconstruir ou editar o horario dos professores em si.
    """
    arquivo = models.FileField(upload_to='horarios_pdf/')
    nome_original = models.CharField(max_length=255, blank=True)
    turno_detectado = models.CharField(max_length=10, choices=TURNO_CHOICES_LAB, blank=True)
    enviado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    enviado_em = models.DateTimeField(auto_now_add=True)
    total_slots_lidos = models.IntegerField(default=0)
    avisos_leitura = models.TextField(blank=True)

    class Meta:
        ordering = ['-enviado_em']
        verbose_name = "PDF de Horario de Professores"
        verbose_name_plural = "PDFs de Horario de Professores"

    def __str__(self):
        return f"{self.nome_original} ({self.get_turno_detectado_display() or 'turno?'}) - {self.enviado_em:%d/%m/%Y %H:%M}"


class ProfessorPdfAlias(models.Model):
    """Vincula o texto exato do cabecalho de professor usado no PDF a um Professor cadastrado.

    Resolvido manualmente uma vez pelo administrador (com sugestao automatica) e
    reaproveitado em todos os uploads seguintes, pois o PDF usa apelidos/abreviacoes
    que nao batem com o nome completo cadastrado.
    """
    texto_pdf = models.CharField(max_length=200, unique=True)
    professor = models.ForeignKey(Professor, on_delete=models.SET_NULL, null=True, blank=True)
    ignorar = models.BooleanField(default=False, help_text="Ex.: cabecalho 'AGUARDANDO PROFESSOR'")
    atualizado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['texto_pdf']
        verbose_name = "Vinculo de Professor do PDF"
        verbose_name_plural = "Vinculos de Professores do PDF"

    def __str__(self):
        return f"{self.texto_pdf} -> {self.professor or ('ignorado' if self.ignorar else 'pendente')}"


class TurmaPdfAlias(models.Model):
    """Vincula o texto de turma usado no PDF a uma Turma cadastrada, nos mesmos moldes do ProfessorPdfAlias."""
    texto_pdf = models.CharField(max_length=200, unique=True)
    turma = models.ForeignKey(Turma, on_delete=models.SET_NULL, null=True, blank=True)
    atualizado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['texto_pdf']
        verbose_name = "Vinculo de Turma do PDF"
        verbose_name_plural = "Vinculos de Turmas do PDF"

    def __str__(self):
        return f"{self.texto_pdf} -> {self.turma or 'pendente'}"


class DivergenciaCronogramaPdf(models.Model):
    """Resultado da comparacao de um slot do PDF com o cronograma ativo dos laboratorios.

    Guarda o texto cru do PDF (nao so as FKs resolvidas) de proposito: o relatorio
    continua legivel mesmo que um Professor/Turma seja editado ou excluido depois.
    """
    STATUS_CHOICES = [
        ('confere', 'Confere'),
        ('verificar', 'Verificar'),
        ('conflito', 'Conflito'),
    ]
    DECISAO_CHOICES = [
        ('pendente', 'Pendente'),
        ('mantido', 'Mantido'),
        ('alterado', 'Alterado'),
        ('ignorado', 'Ignorado'),
    ]

    upload = models.ForeignKey(HorarioProfessorPDF, on_delete=models.CASCADE, related_name='divergencias')
    agendamento_lab = models.ForeignKey(AgendamentoLab, on_delete=models.SET_NULL, null=True, blank=True)

    turno = models.CharField(max_length=10, choices=TURNO_CHOICES_LAB)
    dia_semana = models.CharField(max_length=20)
    horario = models.CharField(max_length=1)

    professor_pdf_texto = models.CharField(max_length=200, blank=True)
    professor = models.ForeignKey(Professor, on_delete=models.SET_NULL, null=True, blank=True)

    turma_pdf_texto = models.CharField(max_length=200, blank=True)
    turma = models.ForeignKey(Turma, on_delete=models.SET_NULL, null=True, blank=True)

    disciplina_pdf_texto = models.CharField(max_length=200, blank=True)
    laboratorio = models.ForeignKey(Laboratorio, on_delete=models.SET_NULL, null=True, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    detalhe = models.TextField(blank=True)

    sugestao_dia = models.CharField(max_length=20, blank=True)
    sugestao_horario = models.CharField(max_length=1, blank=True)

    decisao = models.CharField(max_length=10, choices=DECISAO_CHOICES, default='pendente')
    decidido_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    decidido_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-upload_id', 'turno', 'dia_semana', 'horario']
        verbose_name = "Divergencia do Cronograma (PDF)"
        verbose_name_plural = "Divergencias do Cronograma (PDF)"

    def __str__(self):
        return f"{self.get_status_display()} - {self.professor_pdf_texto} - {self.dia_semana} {self.horario}"


class HistoricoAlteracaoCronogramaLab(models.Model):
    """Auditoria de alteracoes no cronograma de laboratorios originadas da conferencia por PDF.

    Campos guardados como texto puro (nao FKs) de proposito: e um registro de
    auditoria e nao deve quebrar nem perder legibilidade se algo referenciado for
    editado ou excluido depois.
    """
    usuario = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    data_hora = models.DateTimeField(auto_now_add=True)
    upload_pdf = models.ForeignKey(HorarioProfessorPDF, on_delete=models.SET_NULL, null=True, blank=True)

    laboratorio_nome = models.CharField(max_length=20)
    professor_nome = models.CharField(max_length=200)
    turno = models.CharField(max_length=10)

    turma_nome_anterior = models.CharField(max_length=50)
    turma_nome_novo = models.CharField(max_length=50)
    dia_anterior = models.CharField(max_length=20)
    horario_anterior = models.CharField(max_length=1)
    dia_novo = models.CharField(max_length=20)
    horario_novo = models.CharField(max_length=1)
    disciplina_pdf = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-data_hora']
        verbose_name = "Historico de Alteracao do Cronograma (PDF)"
        verbose_name_plural = "Historico de Alteracoes do Cronograma (PDF)"

    def __str__(self):
        return (
            f"{self.laboratorio_nome} - {self.professor_nome} - "
            f"{self.turma_nome_anterior}/{self.dia_anterior} {self.horario_anterior} -> "
            f"{self.turma_nome_novo}/{self.dia_novo} {self.horario_novo}"
        )
