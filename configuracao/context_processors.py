from .models import ConfiguracaoEscola  
  
def escola_context(request):  
    return {  
        'escola': ConfiguracaoEscola.get_solo()  
    } 
