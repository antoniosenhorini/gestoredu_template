from django.db import migrations


TABELA = 'apps_avisoprofessor'
BACKUP = 'apps_avisoprofessor_backup_0025'

COLUNAS_ATUAIS = {
    'id',
    'professor_nome',
    'disciplina',
    'data',
    'tipo',
    'quantidade_dias_atestado',
    'horario_chegada',
    'observacao',
    'criado_em',
    'visualizado',
    'visualizado_por_id',
    'visualizado_em',
}


def _colunas(cursor, tabela):
    cursor.execute(f'PRAGMA table_info("{tabela}")')
    return {linha[1]: {'notnull': bool(linha[3])} for linha in cursor.fetchall()}


def _existe_tabela(cursor, tabela):
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = %s",
        [tabela],
    )
    return cursor.fetchone() is not None


def _primeiro_texto(colunas, nomes, padrao):
    expressoes = [f'NULLIF("{nome}", \'\')' for nome in nomes if nome in colunas]
    if not expressoes:
        return padrao
    return f'COALESCE({", ".join(expressoes)}, {padrao})'


def _primeiro_valor(colunas, nomes, padrao):
    expressoes = [f'"{nome}"' for nome in nomes if nome in colunas]
    if not expressoes:
        return padrao
    return f'COALESCE({", ".join(expressoes)}, {padrao})'


def _recriar_avisoprofessor(apps, schema_editor):
    if schema_editor.connection.vendor != 'sqlite':
        return

    with schema_editor.connection.cursor() as cursor:
        if not _existe_tabela(cursor, TABELA):
            return

        colunas = _colunas(cursor, TABELA)
        nomes_colunas = set(colunas)
        schema_ok = (
            COLUNAS_ATUAIS.issubset(nomes_colunas)
            and 'email_professor' not in nomes_colunas
        )
        if schema_ok:
            return

        cursor.execute(f'CREATE TABLE IF NOT EXISTS "{BACKUP}" AS SELECT * FROM "{TABELA}"')
        cursor.execute(f'DROP TABLE IF EXISTS "{TABELA}_novo"')
        cursor.execute(
            f'''
            CREATE TABLE "{TABELA}_novo" (
                "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT,
                "professor_nome" varchar(150) NOT NULL,
                "disciplina" varchar(100) NOT NULL,
                "data" date NOT NULL,
                "tipo" varchar(20) NOT NULL,
                "quantidade_dias_atestado" integer unsigned NULL,
                "horario_chegada" time NULL,
                "observacao" text NULL,
                "criado_em" datetime NOT NULL,
                "visualizado" bool NOT NULL,
                "visualizado_em" datetime NULL,
                "visualizado_por_id" integer NULL REFERENCES "auth_user" ("id") DEFERRABLE INITIALLY DEFERRED
            )
            '''
        )

        id_expr = '"id"' if 'id' in nomes_colunas else 'rowid'
        professor_expr = _primeiro_texto(nomes_colunas, ['professor_nome', 'nome_professor'], "''")
        data_expr = _primeiro_valor(nomes_colunas, ['data', 'data_aviso'], "date('now')")
        tipo_base = _primeiro_texto(nomes_colunas, ['tipo', 'tipo_aviso'], "'FALTA'")
        tipo_expr = (
            f"CASE UPPER({tipo_base}) "
            "WHEN 'ATESTADO' THEN 'ATESTADO' "
            "WHEN 'ATRASO' THEN 'ATRASO' "
            "WHEN 'FALTA' THEN 'FALTA' "
            "ELSE 'FALTA' END"
        )
        quantidade_expr = _primeiro_valor(
            nomes_colunas,
            ['quantidade_dias_atestado', 'dias_atestado'],
            'NULL',
        )
        horario_expr = _primeiro_valor(nomes_colunas, ['horario_chegada'], 'NULL')
        observacao_expr = _primeiro_valor(nomes_colunas, ['observacao', 'descricao'], 'NULL')
        criado_em_expr = _primeiro_valor(nomes_colunas, ['criado_em'], "datetime('now')")
        visualizado_expr = _primeiro_valor(nomes_colunas, ['visualizado'], '0')
        visualizado_em_expr = _primeiro_valor(nomes_colunas, ['visualizado_em'], 'NULL')
        visualizado_por_base = _primeiro_valor(nomes_colunas, ['visualizado_por_id'], 'NULL')
        visualizado_por_expr = (
            f'CASE WHEN {visualizado_por_base} IN (SELECT "id" FROM "auth_user") '
            f'THEN {visualizado_por_base} ELSE NULL END'
        )

        cursor.execute(
            f'''
            INSERT INTO "{TABELA}_novo" (
                "id",
                "professor_nome",
                "disciplina",
                "data",
                "tipo",
                "quantidade_dias_atestado",
                "horario_chegada",
                "observacao",
                "criado_em",
                "visualizado",
                "visualizado_em",
                "visualizado_por_id"
            )
            SELECT
                {id_expr},
                {professor_expr},
                '',
                {data_expr},
                {tipo_expr},
                {quantidade_expr},
                {horario_expr},
                {observacao_expr},
                {criado_em_expr},
                {visualizado_expr},
                {visualizado_em_expr},
                {visualizado_por_expr}
            FROM "{TABELA}"
            '''
        )
        cursor.execute(f'DROP TABLE "{TABELA}"')
        cursor.execute(f'ALTER TABLE "{TABELA}_novo" RENAME TO "{TABELA}"')
        cursor.execute(
            f'CREATE INDEX IF NOT EXISTS "{TABELA}_visualizado_por_id_0025_idx" '
            f'ON "{TABELA}" ("visualizado_por_id")'
        )


class Migration(migrations.Migration):

    dependencies = [
        ('apps', '0024_avisoprofessor'),
    ]

    operations = [
        migrations.RunPython(_recriar_avisoprofessor, migrations.RunPython.noop),
    ]
