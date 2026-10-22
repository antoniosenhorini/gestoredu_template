# AGENTS.md

## Ambiente

ATENÇÃO: ESTE SISTEMA ESTÁ EM PRODUÇÃO.

Servidor:

* Ubuntu 24.04
* Django 6
* Gunicorn
* Nginx
* HTTPS ativo

Projeto:

* /dados/escola_zanchim

## Regras obrigatórias

* Nunca modificar arquivos sem explicar o plano antes.

* Nunca executar comandos destrutivos.

* Nunca executar:

  * rm -rf
  * flush
  * migrate
  * makemigrations
  * collectstatic
  * reset_db
    sem autorização explícita.

* Nunca alterar:

  * models.py
  * settings.py
  * urls.py
  * banco de dados
    sem autorização explícita.

* Nunca remover código existente sem justificar.

## Fluxo de trabalho

1. Ler os arquivos necessários.
2. Explicar o problema encontrado.
3. Apresentar um plano detalhado.
4. Aguardar aprovação.
5. Aplicar alterações.
6. Mostrar arquivos modificados.
7. Executar apenas:

python3 manage.py check

8. Relatar resultado.

## Git

Antes de alterações grandes:

git status

Se houver risco:

sugerir criação de tag ou branch.

## Áreas protegidas

Não modificar sem autorização:

* apps/controllers/alunos
* apps/controllers/livros
* apps/controllers/top10

## Templates

Priorizar:

* Bootstrap
* Responsividade
* Reutilização de includes

## Segurança

Não expor:

* Tokens
* Senhas
* API Keys
* Credenciais

## Produção

Assumir sempre que:

* Há usuários utilizando o sistema.
* Qualquer erro pode afetar a operação da escola.
* Mudanças devem ser mínimas e reversíveis.

