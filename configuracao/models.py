from django.db import models  
  
class ConfiguracaoEscola(models.Model):  
    nome = models.CharField(max_length=200, default="Nome do Colegio")  
    sigla = models.CharField(max_length=20, blank=True)
    endereco = models.CharField(max_length=255, blank=True, verbose_name="Endereço")
    telefone = models.CharField(max_length=20, blank=True, verbose_name="Telefone")
    email = models.EmailField(blank=True, verbose_name="E-mail")  
    logo = models.ImageField(upload_to='logos/', blank=True, null=True)  
    cor_primaria = models.CharField(max_length=7, default='#0f2744')  
    setup_concluido = models.BooleanField(default=False)  
    criado_em = models.DateTimeField(auto_now_add=True)  
    atualizado_em = models.DateTimeField(auto_now=True)  
  
    class Meta:  
        verbose_name = "Configuracao da Escola"  
        verbose_name_plural = "Configuracao da Escola"  
  
    def __str__(self):  
        return self.nome  
  
    @classmethod  
    def get_solo(cls):  
        obj, created = cls.objects.get_or_create(pk=1)  
        return obj 
