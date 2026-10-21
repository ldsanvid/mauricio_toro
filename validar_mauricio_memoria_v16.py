"""Validador LOCAL de arquitectura V16: no consulta S3 ni envía Telegram.

Ejecutar en la PC, no en el Render Background Worker.
"""
import ast
import importlib.util
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
FILES = [
    'mauricio_worker_supervisor_v16_mem.py',
    'google_news_rss_only_v16.py',
    'news_s3_store_mem_v16.py',
]
for file_name in FILES:
    path = BASE / file_name
    if not path.is_file():
        raise AssertionError(f'Falta archivo: {path}')
    ast.parse(path.read_text(encoding='utf-8'), filename=file_name)
    print(f'OK | sintaxis {file_name}')

rss_source = (BASE / FILES[1]).read_text(encoding='utf-8')
assert 'from news_s3_store_mem_v16 import (' in rss_source
assert 'from x_worker_mauricio_v2 import ' not in rss_source
assert 'from youtube_worker_mauricio_v6 import ' not in rss_source
assert 'from news_brief_mauricio_ejes_v17 import ' not in rss_source
assert 'def send_pending_telegrams(' in rss_source
assert 'def process_source(' in rss_source
print('OK | RSS conserva alertas, pero no importa X/YouTube/brief')

spec = importlib.util.spec_from_file_location('supervisor_v16_test', BASE / FILES[0])
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)
original_stage = supervisor.stage
original_due = supervisor.x_due
original_save = supervisor.save_x_run
try:
    calls = []
    supervisor.stage = lambda label, script, *args: calls.append((label,script,args)) or 0
    supervisor.x_due = lambda: True
    supervisor.save_x_run = lambda: calls.append(('mark_x_done', '', ()))
    supervisor.one_cycle()
    expected = ['GOOGLE NEWS + ALERTAS', 'X', 'mark_x_done', 'YOUTUBE', 'BRIEF']
    assert [label for label,_,_ in calls] == expected, calls
    assert calls[0][2] == ('--once',)
    assert calls[-1][2] == ('--run',)
    print('OK | etapas secuenciales Google News→X→YouTube→Brief')

    calls.clear()
    supervisor.x_due = lambda: False
    supervisor.one_cycle()
    assert [label for label,_,_ in calls] == ['GOOGLE NEWS + ALERTAS','YOUTUBE','BRIEF']
    print('OK | X respeta su intervalo y se omite cuando no corresponde')

    calls.clear()
    supervisor.x_due = lambda: True
    supervisor.stage = lambda label, script, *args: calls.append(label) or (137 if label=='GOOGLE NEWS + ALERTAS' else 0)
    supervisor.one_cycle()
    assert calls == ['GOOGLE NEWS + ALERTAS']
    print('OK | RSS fallido no dispara otras etapas')
finally:
    supervisor.stage = original_stage
    supervisor.x_due = original_due
    supervisor.save_x_run = original_save

print('✅ VALIDACIÓN LOCAL V16 TERMINADA | sin S3, Telegram ni acceso a internet.')
