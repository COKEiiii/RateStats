"""Bounded text retry. Never changes source values or accepts a failed response."""
import hashlib, shutil, tempfile
from datetime import datetime
from pathlib import Path
from .common import load,save,digest,confined

def source_cell_cuts(boxes,pixel_width):
    if len(boxes)<2 or any(not b or b['width']<=0 or b['height']<=0 for b in boxes):return None
    first=boxes[0];end=boxes[-1]['x']+boxes[-1]['width']
    for left,right in zip(boxes,boxes[1:]):
        if abs(left['x']+left['width']-right['x'])>1:return None
        if abs(right['y']-first['y'])>1 or abs(right['height']-first['height'])>1:return None
    scale=pixel_width/(end-first['x'])
    cuts=[round((b['x']-first['x'])*scale) for b in boxes[1:]]
    return cuts if [0,*cuts,pixel_width]==sorted(set([0,*cuts,pixel_width])) else None


def eligible(error):
    return any(x in str(error).lower() for x in ('literal source/transcription mismatch','token repeat limit','incomplete model response','expecting value','expecting delimiter','expecting property name','unterminated string','context budget'))


def repair(root,stage):
    root=Path(root);run=load(root/'run.json');proof=load(root/'wave-checks.json')
    if proof['task_hash']!=digest(run['tasks']):raise ValueError('修复停止：核验记录与当前来源不匹配')
    tasks={t['id']:t for t in run['tasks']}
    failed={e['task'] for e in proof.get('errors',[]) if e.get('lane')=='llm' and eligible(e.get('error',''))}
    for tid in failed:
        for name,sha in tasks[tid].get('image_hashes',{}).items():
            if hashlib.sha256(confined(root/'evidence',name).read_bytes()).hexdigest()!=sha:raise ValueError('修复停止：原始证据已变化')
    for name,sha in run.get('source_file_hashes',{}).items():
        if hashlib.sha256(confined(root/'evidence',name).read_bytes()).hexdigest()!=sha:raise ValueError('修复停止：原始来源已变化')
    actions=[];unresolved=[]
    if not failed:return dict(stage=stage,actions=[],unresolved=[],archive=None)
    archive=Path(tempfile.mkdtemp(prefix='text-retry-original-',dir=root))
    for name in ('run.json','wave-checks.json'):shutil.copy2(root/name,archive/name)
    for tid in sorted(failed):
        task=tasks[tid]
        if stage=='layout':
            flag='strict_row_layout' if task['kind']=='grid' else 'strict_literal_text'
            if not task.get(flag):task[flag]=True;actions.append(dict(task=tid,method='明确文字表格结构' if task['kind']=='grid' else '逐字完整转录'))
        elif stage=='source' and not task.get('text_chunk_size'):
            task['text_chunk_size']=600;task['text_rows_per_request']=1
            actions.append(dict(task=tid,method='原始文字分段重读；不改来源值'))
        else:unresolved.append(dict(task=tid,reason='已完成限定重试，需人工核实原始文字'))
    run['task_hash']=digest(run['tasks']);save(root/'run.json',run)
    return dict(stage=stage,actions=actions,unresolved=unresolved,archive=str(archive))
