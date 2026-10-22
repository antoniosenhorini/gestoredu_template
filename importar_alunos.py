#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import django

# Configurar o Django
sys.path.append('/home/gestaoluizzanchim')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'colegio.settings')
django.setup()

import pandas as pd
from apps.models import Aluno, Turma
import re

def limpar_telefone(tel):
    """Remove caracteres especiais do telefone"""
    if pd.isna(tel):
        return ''
    tel_str = str(tel).strip()
    # Remove tudo que não é número
    return re.sub(r'[^0-9]', '', tel_str)

def importar_alunos(arquivo):
    """Importa alunos de um arquivo Excel com abas 'manha' e 'tarde'"""
    
    # Estatísticas
    stats = {
        'manha': {'novos': 0, 'atualizados': 0, 'turmas': set()},
        'tarde': {'novos': 0, 'atualizados': 0, 'turmas': set()}
    }
    
    # Abrir o arquivo
    xls = pd.ExcelFile(arquivo)
    
    for turno in ['manha', 'tarde']:
        print(f"\n{'='*50}")
        print(f"📚 IMPORTANDO TURNO: {turno.upper()}")
        print(f"{'='*50}")
        
        if turno not in xls.sheet_names:
            print(f"⚠️ Aba '{turno}' não encontrada! Pulando...")
            continue
        
        # Ler com cabeçalho
        df = pd.read_excel(xls, sheet_name=turno)
        
        for index, row in df.iterrows():
            try:
                turma_nome = str(row['Turma']).strip()
                numero = int(row['Nº'])
                nome = str(row['Nome do Estudante']).strip()
                telefone = limpar_telefone(row['Telefone'])
                
                # Buscar ou criar turma (com ano)
                turma, created = Turma.objects.get_or_create(
                    nome=turma_nome,
                    defaults={
                        'turno': turno,
                        'ativa': True,
                        'ano': 2026,
                        'serie': turma_nome[0] + 'º Ano' if turma_nome[0].isdigit() else 'Ensino Médio'
                    }
                )
                
                # Se a turma já existia, atualizar o turno se necessário
                if not created and turma.turno != turno:
                    turma.turno = turno
                    turma.save()
                    print(f"  🔄 Turma {turma_nome} atualizada para turno {turno}")
                
                stats[turno]['turmas'].add(turma_nome)
                
                # Criar ou atualizar aluno (com telefone)
                aluno, created = Aluno.objects.update_or_create(
                    turma=turma,
                    numero=numero,
                    defaults={
                        'nome': nome,
                        'telefone': telefone,
                        'ativo': True
                    }
                )
                
                if created:
                    stats[turno]['novos'] += 1
                    print(f"  ✅ NOVO: {turma_nome} - {numero} - {nome[:30]}")
                else:
                    stats[turno]['atualizados'] += 1
                    
            except Exception as e:
                print(f"  ❌ ERRO na linha {index+2}: {e}")
        
        print(f"\n📊 RESUMO {turno.upper()}:")
        print(f"   - Novos alunos: {stats[turno]['novos']}")
        print(f"   - Atualizados: {stats[turno]['atualizados']}")
        print(f"   - Turmas: {', '.join(sorted(stats[turno]['turmas']))}")
    
    # Relatório final
    print(f"\n{'='*50}")
    print(f"🎯 RELATÓRIO FINAL")
    print(f"{'='*50}")
    print(f"✅ Total de alunos importados: {stats['manha']['novos'] + stats['tarde']['novos']}")
    print(f"🔄 Total de alunos atualizados: {stats['manha']['atualizados'] + stats['tarde']['atualizados']}")
    print(f"📚 Total de turmas: {len(stats['manha']['turmas'] | stats['tarde']['turmas'])}")
    print(f"   - Manhã: {len(stats['manha']['turmas'])} turmas")
    print(f"   - Tarde: {len(stats['tarde']['turmas'])} turmas")

if __name__ == "__main__":
    arquivo = '/home/gestaoluizzanchim/cadastro_geral.xls'
    
    if not os.path.exists(arquivo):
        print(f"❌ Arquivo não encontrado: {arquivo}")
        print("Verifique o caminho do arquivo.")
    else:
        print(f"📁 Arquivo encontrado: {arquivo}")
        importar_alunos(arquivo)