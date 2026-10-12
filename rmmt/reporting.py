from datetime import datetime,timezone
from pathlib import Path
import csv,json,hashlib,uuid
from decimal import Decimal
from .config import local_path
from .data import write_json
from . import __version__

def csv_file(path,rows,fields=None):
    rows=list(rows)
    if fields is None:fields=list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w',newline='',encoding='utf-8-sig') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields or ['empty'])
        writer.writeheader()
        for row in rows:writer.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()})

def export_report(config,signal=None,plan=None,ledger=None,snapshot=None,summary=None):
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'_'+uuid.uuid4().hex[:8]
    output=local_path(f'{config.results_dir}/{config.strategy_id}/{run_id}');output.mkdir(parents=True)
    write_json(output/'config.json',config.public()|{'config_hash':config.hash})
    code_hash=hashlib.sha256(b''.join(p.read_bytes() for p in sorted(local_path('rmmt').glob('*.py')))+local_path('main_rmmt.py').read_bytes()).hexdigest()
    write_json(output/'manifest.json',{'spec_version':'0.6','code_version':__version__,'code_hash':code_hash,
        'mode':config.mode,'strategy':config.strategy,'strategy_id':config.strategy_id,
        'run_id':run_id,'created_at':datetime.now(timezone.utc).isoformat(),
        'snapshot_hash':hashlib.sha256(json.dumps(snapshot,sort_keys=True).encode()).hexdigest() if snapshot else None,
        'source_quality':(snapshot or {}).get('quality'),'filter_version':'regulatory5_configurable_causal35_v2',
        'execution_policy_version':'window30_no_chase_v2','signal_price_basis':'KIS_ADJUSTED',
        'data_contract_version':2,'simulated':config.mode=='PAPER'})
    if signal:
        write_json(output/'signal.json',signal)
        csv_file(output/'signals.csv',signal['candidates'])
        csv_file(output/'filter_decisions.csv',signal['decisions'])
    if plan:
        write_json(output/'plan.json',plan)
        csv_file(output/'rebalance_plan.csv',plan.get('sells',[])+plan.get('buys',[]))
    if ledger:
        csv_file(output/'orders.csv',({**o,'requested_qty':o['quantity'],'filled_qty':o['filled'],'broker_order_id':o['broker_id']} for o in ledger.orders()))
        for table,filename in [('fills','trades'),('cash_events','cash_events'),('quantity_events','quantity_events'),
                               ('valuation_events','valuation_events'),('nav','daily_nav'),('audit','audit')]:
            rows=[dict(r) for r in ledger.db.execute('SELECT * FROM '+table)]
            if table=='fills':
                for row in rows:
                    row['fill_id']=row['id'];row['actual_or_simulated']='SIMULATED' if row['simulated'] else 'ACTUAL'
                    row['price']=str(Decimal(row['gross'])/row['qty']) if row['qty'] else None
                    row['cost_status']='ESTIMATED_OR_OPERATOR_RECONCILED'
            if table=='nav':
                for row in rows:
                    row['date']=row['day'];row['twr']=str(Decimal(row['normalized_nav'])-1)
            csv_file(output/(filename+'.csv'),rows)
        equity=ledger.cash+sum((p['quantity']*Decimal(p['mark']) for p in ledger.positions().values()),Decimal(0))
        csv_file(output/'holdings.csv',({**p,'legal_qty':p['quantity'],'cost_basis':p['cost'],
            'valuation_price':p['mark'],'value':str(p['quantity']*Decimal(p['mark'])),
            'weight':str(p['quantity']*Decimal(p['mark'])/equity) if equity else None,'status':p['valuation_status']} for p in ledger.positions().values()))
        write_json(output/'validation.json',ledger.validate())
    else:write_json(output/'validation.json',{'passed':True,'scope':'signal_and_plan_only','broker_checked':False})
    write_json(output/'summary.json',summary or {'mode':config.mode,'signal_id':signal.get('signal_id') if signal else None,
        'candidate_count':len(signal['candidates']) if signal else None,'broker_checked':False})
    return output
