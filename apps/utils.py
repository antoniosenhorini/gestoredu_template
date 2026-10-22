from .services.whatsapp_service import enviar_mensagem_whatsapp


def enviar_whatsapp(telefone, mensagem):
    """Compatibilidade com imports antigos; use enviar_mensagem_whatsapp em codigo novo."""
    return enviar_mensagem_whatsapp(telefone, mensagem)
