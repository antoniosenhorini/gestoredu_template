import re

import requests
from django.conf import settings


def formatar_numero_whatsapp(numero):
    """Normaliza telefone para o formato internacional usado pela Cloud API."""
    if not numero:
        return ''

    numero_limpo = re.sub(r'\D+', '', str(numero))
    if not numero_limpo:
        return ''

    if numero_limpo.startswith('00'):
        numero_limpo = numero_limpo[2:]

    if not numero_limpo.startswith('55'):
        numero_limpo = f'55{numero_limpo}'

    return numero_limpo


def _resultado(status, resposta=None, erro=None, numero=None, status_code=None):
    return {
        'status': status,
        'resposta': resposta,
        'erro': erro,
        'numero': numero,
        'status_code': status_code,
    }


def enviar_mensagem_whatsapp(numero, mensagem):
    """
    Envia mensagem de texto pela WhatsApp Cloud API.

    Retorna sempre um dicionario com status, resposta JSON e erro quando houver.
    Falhas de configuracao, validacao ou rede nao levantam excecao para o fluxo
    chamador.
    """
    numero_formatado = formatar_numero_whatsapp(numero)
    mensagem = (mensagem or '').strip()

    if not numero_formatado:
        return _resultado(False, erro='Numero de WhatsApp vazio ou invalido.')

    if not mensagem:
        return _resultado(False, numero=numero_formatado, erro='Mensagem vazia.')

    token = getattr(settings, 'WHATSAPP_TOKEN', '') or ''
    phone_number_id = getattr(settings, 'PHONE_NUMBER_ID', '') or ''
    api_version = getattr(settings, 'WHATSAPP_API_VERSION', 'v20.0') or 'v20.0'

    if not token or not phone_number_id:
        return _resultado(
            False,
            numero=numero_formatado,
            erro='WHATSAPP_TOKEN e PHONE_NUMBER_ID precisam estar configurados.',
        )

    url = f'https://graph.facebook.com/{api_version}/{phone_number_id}/messages'
    payload = {
        'messaging_product': 'whatsapp',
        'to': numero_formatado,
        'type': 'text',
        'text': {
            'preview_url': False,
            'body': mensagem,
        },
    }
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
    except requests.RequestException as exc:
        return _resultado(False, numero=numero_formatado, erro=str(exc))

    try:
        resposta_json = response.json()
    except ValueError:
        resposta_json = {'raw': response.text}

    if response.ok:
        return _resultado(True, resposta=resposta_json, numero=numero_formatado, status_code=response.status_code)

    erro = resposta_json.get('error') if isinstance(resposta_json, dict) else resposta_json
    return _resultado(False, resposta=resposta_json, erro=erro, numero=numero_formatado, status_code=response.status_code)


def enviar_template_whatsapp(numero, nome_template, idioma, parametros):
    """Envia um template aprovado pela WhatsApp Cloud API."""
    numero_formatado = formatar_numero_whatsapp(numero)

    if not numero_formatado:
        return _resultado(False, erro='Numero de WhatsApp vazio ou invalido.')

    token = getattr(settings, 'WHATSAPP_TOKEN', '') or ''
    phone_number_id = getattr(settings, 'PHONE_NUMBER_ID', '') or ''
    api_version = getattr(settings, 'WHATSAPP_API_VERSION', 'v20.0') or 'v20.0'

    if not token or not phone_number_id:
        return _resultado(
            False,
            numero=numero_formatado,
            erro='WHATSAPP_TOKEN e PHONE_NUMBER_ID precisam estar configurados.',
        )

    componentes = []
    if parametros:
        componentes.append({
            'type': 'body',
            'parameters': [
                {
                    'type': 'text',
                    'text': str(parametro),
                }
                for parametro in parametros
            ],
        })

    url = f'https://graph.facebook.com/{api_version}/{phone_number_id}/messages'
    payload = {
        'messaging_product': 'whatsapp',
        'to': numero_formatado,
        'type': 'template',
        'template': {
            'name': nome_template,
            'language': {
                'code': idioma,
            },
            'components': componentes,
        },
    }
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
    except requests.RequestException as exc:
        return _resultado(False, numero=numero_formatado, erro=str(exc))

    try:
        resposta_json = response.json()
    except ValueError:
        resposta_json = {'raw': response.text}

    if response.ok:
        return _resultado(True, resposta=resposta_json, numero=numero_formatado, status_code=response.status_code)

    erro = resposta_json.get('error') if isinstance(resposta_json, dict) else resposta_json
    return _resultado(False, resposta=resposta_json, erro=erro, numero=numero_formatado, status_code=response.status_code)


def enviar_template_aviso_falta_aluno(numero, nome_aluno, data_falta):
    return enviar_template_whatsapp(
        numero=numero,
        nome_template='aviso_falta_aluno',
        idioma='pt_BR',
        parametros=[nome_aluno, data_falta],
    )


def enviar_template_aviso_ocorrencia_aluno(numero, nome_aluno, tipo_ocorrencia, data_ocorrencia):
    return enviar_template_whatsapp(
        numero=numero,
        nome_template='aviso_ocorrencia_aluno',
        idioma='pt_BR',
        parametros=[nome_aluno, tipo_ocorrencia, data_ocorrencia],
    )


def enviar_template_relatorio_semanal_faltas(
    numero,
    nome_aluno,
    total_faltas,
    total_aulas,
    telefone_escola,
    data_envio,
):
    return enviar_template_whatsapp(
        numero=numero,
        nome_template='relatorio_semanal_faltas',
        idioma='pt_BR',
        parametros=[nome_aluno, total_faltas, total_aulas, telefone_escola, data_envio],
    )


def enviar_template_relatorio_semanal_ocorrencias(
    numero,
    nome_aluno,
    total_ocorrencias,
    lista_ocorrencias,
    telefone_escola,
    data_envio,
):
    return enviar_template_whatsapp(
        numero=numero,
        nome_template='relatorio_semanal_ocorrencias',
        idioma='pt_BR',
        parametros=[nome_aluno, total_ocorrencias, lista_ocorrencias, telefone_escola, data_envio],
    )
