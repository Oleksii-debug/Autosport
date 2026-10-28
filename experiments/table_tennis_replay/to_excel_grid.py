"""Convert the real captured recording to a Ukrainian row-by-match Excel grid."""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

source=Path(sys.argv[1]); out=Path(sys.argv[2]); data=json.loads(source.read_text('utf-8'))
names={'Artur':'Артур','Grela':'Греля','Marcin':'Марцін','Jadczyk':'Ядчик','Pawel':'Павел','Adamus':'Адамус','Dawid':'Давід','Dytko':'Дитко','Serhii':'Сергій','Skachenko':'Скаченко','Oleksandr':'Олександр','Naida':'Найда','Kostiantyn':'Костянтин','Hrynenko':'Гриненко','Andrii':'Андрій','Peretiatko':'Перетятько','Rafal':'Рафал','Gajda':'Гайда','Jerzy':'Єжи','Pelka':'Пелка','Petr':'Петр','David':'Давид','Szymon':'Шимон','Kolasa':'Коласа','Tymur':'Тимур','Ponomarenko':'Пономаренко','Roman':'Роман','Hnoievoi':'Гноєвий','Mykola':'Микола','Treshchetka':'Трещетка','Mykhailo':'Михайло','Dunas':'Дунас','Kacper':'Кацпер','Slawinski':'Славінський','Michna':'Міхна','Ziakun':'Зякун','Chuhai':'Чугай','Nataliia':'Наталія','Plokhotniuk':'Плохотнюк','Alina':'Аліна','Kurtenko':'Куртенко','Vasyl':'Василь','Verbianyi':'Вербяний','Nazar':'Назар','Danyliuk':'Данилюк','Jakub':'Якуб','Nowak':'Новак','Adrian':'Адріан','Wiecek':'Венцек','Valerii':'Валерій','Hel':'Гель','Vivatenko':'Віватенко','Pohorilyi':'Погорілий','Onykyienko':'Онікієнко','Syksa':'Сикса','Oleh':'Олег','Kurylenko':'Куриленко','Daria':'Дарія','Kovalova':'Ковальова','Ovriakh':'Оврях','Dmytro':'Дмитро','Zeniuk':'Зенюк','Anton':'Антон','Molochko':'Молочко'}
def ua(name): return ' '.join(names.get(w,w) for w in name.split())
def odds(o):
 vals=[]
 for k in ('home','away'):
  p=(o or {}).get(k) or {};v=p.get('odds')
  vals.append(('%.2f'%v).replace('.',',') if isinstance(v,(int,float)) and v>1 and not p.get('suspended') and not p.get('stale') else 'немає')
 return ' / '.join(vals)
def score(s):
 if not isinstance(s,str) or not s.strip(): return 'Сети й очки: немає даних'
 parts=s.split(','); games=[0,0]; current=None
 for p in parts:
  x=re.fullmatch(r'\s*(\d+)\s*[-:]\s*(\d+)\s*',p)
  if not x:return 'Сети й очки: незрозумілий рахунок'
  a,b=map(int,x.groups())
  if max(a,b)>=11 and abs(a-b)>=2:games[0 if a>b else 1]+=1
  else:current=(a,b)
 return f'Сети: {games[0]}:{games[1]}\nОчки: {current[0]}:{current[1]}' if current else f'Сети: {games[0]}:{games[1]}\nОчки: немає поточного сету'
def stamp(s):return datetime.fromisoformat(s.replace('Z','+00:00'))
matches=data['matches'];max_offset=max((f['offset_seconds'] for m in matches for f in m.get('frames',[]) if f.get('kind')!='final'),default=0)
max_offset=max(180, max_offset); headers=['Матч','До початку']+[f'{n} хвилин' for n in range(0,max_offset//60+1,3)]+['Кінець матчу'];rows=[]
for m in matches:
 row=[ua(m['home'])+' — '+ua(m['away']),'Коефіцієнти: '+odds(m.get('prematch',{}).get('odds'))+'\nДо початку гри']
 frames={f['offset_seconds']:f for f in m.get('frames',[]) if f.get('kind')!='final'}
 actual_end=None
 if m.get('first_live_at') and (m.get('result') or {}).get('source_finished_at'):
  actual_end=(stamp(m['result']['source_finished_at'])-stamp(m['first_live_at'])).total_seconds()
 for sec in range(0,max_offset+1,180):
  f=frames.get(sec)
  if actual_end is not None and sec>actual_end: row.append('Матч завершився');continue
  if not f: row.append('Ще не записано' if not m.get('result') else 'Пропущено');continue
  if f['kind']=='missing':row.append('Пропущено');continue
  row.append('Коефіцієнти: '+odds(f.get('odds'))+'\n'+score(f.get('score')))
 result=m.get('result') or {}; period=result.get('periods') or []; final=result.get('final_score')
 if result:
  set_scores=[str(x.get('score','')) for x in period if x.get('score')]
  if set_scores:
   summary=score(','.join(set_scores)).replace('\nОчки: немає поточного сету','')
   row.append(summary+'\nПо сетах: '+', '.join(set_scores))
  elif final:
   row.append('Підсумковий рахунок: '+final+'\nРахунок окремих сетів: немає даних')
  else:row.append('Завершення: немає рахунку')
 else:row.append('Матч ще не завершився')
 rows.append(row)
out.write_text(json.dumps({'headers':headers,'rows':rows,'note':'Перші числа належать першому гравцю, другі — другому. Нульова хвилина: перше спостереження наживо. Зрізи можуть запізнюватися.'},ensure_ascii=False),'utf-8')
