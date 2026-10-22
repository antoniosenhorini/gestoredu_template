"""Associacao entre o texto do PDF (professor/turma) e os cadastros do sistema.

O PDF de horario usa apelidos/abreviacoes que nao batem literalmente com
`Professor.nome_completo` nem com `Turma.nome`. Por isso a resolucao usa uma
tabela de "de-para" (`ProfessorPdfAlias` / `TurmaPdfAlias`) confirmada
manualmente uma vez pelo administrador (com sugestao automatica) e reaproveitada
em todos os uploads seguintes - nunca decide sozinho um vinculo ambiguo.
"""
import difflib
import re
import unicodedata

from ..models import Professor, ProfessorPdfAlias, Turma, TurmaPdfAlias

_RAIZES_CURSO = ['AGRO', 'SIST', 'HUM', 'EXA']
_PADRAO_SERIE_LETRA = re.compile(r'(?P<serie>\d)\s*(?P<letra>[A-F])\b')


def normalizar(texto):
    if not texto:
        return ''
    # 'º'/'ª' (indicadores ordinais) decompoem para 'o'/'a' via NFKD, o que criaria
    # letras falsas coladas no numero (ex.: "1º C" -> "1o C"). Tratamos como espaço antes.
    texto = texto.replace('º', ' ').replace('ª', ' ')
    texto = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode('ascii')
    texto = texto.upper()
    texto = re.sub(r'[^A-Z0-9 ]+', ' ', texto)
    texto = re.sub(r'\s+', ' ', texto).strip()
    return texto


# ---------------------------------------------------------------------------
# Professor
# ---------------------------------------------------------------------------

def sugerir_professor(texto_pdf, professores=None):
    """Sugestao (nao vinculante) de Professor para um texto de PDF. None se ambiguo/nao encontrado."""
    alvo = normalizar(texto_pdf)
    if not alvo:
        return None

    if professores is None:
        professores = list(Professor.objects.filter(ativo=True))

    palavras_alvo = set(alvo.split(' '))
    candidatos = []
    nomes_normalizados = {}
    for professor in professores:
        nome_normalizado = normalizar(professor.nome_completo)
        nomes_normalizados[nome_normalizado] = professor
        palavras_nome = set(nome_normalizado.split(' '))
        if palavras_alvo and palavras_alvo.issubset(palavras_nome):
            candidatos.append(professor)

    if len(candidatos) == 1:
        return candidatos[0]
    if len(candidatos) > 1:
        return None

    proximos = difflib.get_close_matches(alvo, nomes_normalizados.keys(), n=1, cutoff=0.75)
    if proximos:
        return nomes_normalizados[proximos[0]]
    return None


def resolver_professor_pdf(texto_pdf, usuario=None):
    """Retorna (professor_ou_none, ignorar, alias).

    Cria um alias pendente (sem professor confirmado) na primeira vez que um
    texto de heading e visto - a comparacao so usa o vinculo depois que o
    administrador confirmar na tela de vinculos pendentes.
    """
    texto_pdf = (texto_pdf or '').strip()
    if not texto_pdf:
        return None, False, None

    alias, _criado = ProfessorPdfAlias.objects.get_or_create(texto_pdf=texto_pdf)

    if alias.ignorar:
        return None, True, alias
    return alias.professor, False, alias


# ---------------------------------------------------------------------------
# Turma
# ---------------------------------------------------------------------------

def _detectar_curso(texto_normalizado):
    for raiz in _RAIZES_CURSO:
        if raiz in texto_normalizado:
            return raiz
    return ''


def extrair_chave_turma(texto):
    """Extrai (serie, letra, curso) de um texto de turma (do PDF ou do cadastro).

    Ex.: "3°C TEC Des. Sistemas" -> ('3', 'C', 'SIST'); "1A HUM" -> ('1', 'A', 'HUM').
    Devolve None se nao conseguir identificar serie+letra.
    """
    texto_normalizado = normalizar(texto)
    match = _PADRAO_SERIE_LETRA.search(texto_normalizado)
    if not match:
        return None
    return (match.group('serie'), match.group('letra'), _detectar_curso(texto_normalizado))


def sugerir_turma(texto_pdf, turmas=None):
    """Sugestao (nao vinculante) de Turma para um texto de PDF, via normalizacao estrutural."""
    chave_pdf = extrair_chave_turma(texto_pdf)
    if not chave_pdf:
        return None

    if turmas is None:
        turmas = Turma.objects.filter(ativa=True)
    turmas = list(turmas)

    candidatos = [turma for turma in turmas if extrair_chave_turma(turma.nome) == chave_pdf]
    if len(candidatos) == 1:
        return candidatos[0]
    if len(candidatos) > 1:
        return None

    # PDF nao trouxe (ou nao reconhecemos) a palavra-chave do curso: se so existir
    # uma turma ativa com essa serie+letra, o vinculo ainda e seguro.
    serie_pdf, letra_pdf, _curso_pdf = chave_pdf
    candidatos_serie_letra = [
        turma for turma in turmas
        if (extrair_chave_turma(turma.nome) or (None, None, None))[:2] == (serie_pdf, letra_pdf)
    ]
    if len(candidatos_serie_letra) == 1:
        return candidatos_serie_letra[0]
    return None


def resolver_turma_pdf(texto_pdf, usuario=None):
    """Retorna (turma_ou_none, alias). Cria alias pendente na primeira vez que o texto aparece."""
    texto_pdf = (texto_pdf or '').strip()
    if not texto_pdf:
        return None, None

    alias, _criado = TurmaPdfAlias.objects.get_or_create(texto_pdf=texto_pdf)
    return alias.turma, alias
