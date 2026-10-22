from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


GRUPO_EQUIPE_DIRETIVA = 'Equipe Diretiva'
GRUPO_DIGITADORES = 'Digitadores'
GRUPO_PROFESSORES = 'Professores'
GRUPO_CONSULTA_CRONOGRAMA = 'Consulta Cronograma'


def _esta_no_grupo(user, nome_grupo):
    return user.is_authenticated and user.groups.filter(name__iexact=nome_grupo).exists()


def is_superuser(user):
    return user.is_authenticated and user.is_superuser


def is_equipe_diretiva(user):
    return _esta_no_grupo(user, GRUPO_EQUIPE_DIRETIVA)


def is_digitador(user):
    return _esta_no_grupo(user, GRUPO_DIGITADORES)


def is_professor(user):
    return _esta_no_grupo(user, GRUPO_PROFESSORES)


def is_consulta_cronograma(user):
    """Grupo somente leitura: consulta do cronograma de laboratorios
    (manha/tarde, semana ativa, semanas historicas, impressao/PDF, Excel) -
    nunca escrita. Equipe Diretiva e superuser ja tem acesso de consulta
    por si so (ver pode_consultar_cronograma_laboratorios), entao este
    grupo e pensado para usuarios que devem ver o cronograma sem nenhum
    outro privilegio administrativo."""
    return _esta_no_grupo(user, GRUPO_CONSULTA_CRONOGRAMA)


def pode_acessar_painel(user):
    return is_superuser(user) or is_equipe_diretiva(user)


def pode_acessar_faltas(user):
    return is_superuser(user) or is_equipe_diretiva(user) or is_digitador(user)


def pode_acessar_ocorrencias(user):
    return is_superuser(user) or is_equipe_diretiva(user) or is_digitador(user)


def pode_acessar_relatorios(user):
    return is_superuser(user) or is_equipe_diretiva(user)


def pode_justificar_faltas(user):
    return is_superuser(user) or is_equipe_diretiva(user)


def pode_acessar_avisos_professores(user):
    return is_superuser(user) or is_equipe_diretiva(user)


def pode_consultar_cronograma_laboratorios(user):
    """Acesso de LEITURA ao cronograma de laboratorios: Equipe Diretiva e
    superuser (que ja podem tudo) mais o grupo Consulta Cronograma
    (somente leitura). Nunca usar esta funcao para autorizar escrita -
    ver pode_acessar_painel/is_equipe_diretiva para isso."""
    return is_superuser(user) or is_equipe_diretiva(user) or is_consulta_cronograma(user)


def permissao_requerida(teste):
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def wrapper(request, *args, **kwargs):
            if not teste(request.user):
                raise PermissionDenied
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


painel_required = permissao_requerida(pode_acessar_painel)
faltas_required = permissao_requerida(pode_acessar_faltas)
ocorrencias_required = permissao_requerida(pode_acessar_ocorrencias)
relatorios_required = permissao_requerida(pode_acessar_relatorios)
justificar_faltas_required = permissao_requerida(pode_justificar_faltas)
avisos_professores_required = permissao_requerida(pode_acessar_avisos_professores)


def permissoes_usuario(request):
    user = request.user
    return {
        'permissoes': {
            'is_superuser': is_superuser(user),
            'is_equipe_diretiva': is_equipe_diretiva(user),
            'is_digitador': is_digitador(user),
            'is_professor': is_professor(user),
            'pode_acessar_painel': pode_acessar_painel(user),
            'pode_acessar_faltas': pode_acessar_faltas(user),
            'pode_acessar_ocorrencias': pode_acessar_ocorrencias(user),
            'pode_acessar_relatorios': pode_acessar_relatorios(user),
            'pode_justificar_faltas': pode_justificar_faltas(user),
            'pode_acessar_avisos_professores': pode_acessar_avisos_professores(user),
            'is_consulta_cronograma': is_consulta_cronograma(user),
            'pode_consultar_cronograma_laboratorios': pode_consultar_cronograma_laboratorios(user),
        }
    }
