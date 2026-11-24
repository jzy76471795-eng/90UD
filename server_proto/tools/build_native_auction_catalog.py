"""Create a PVF-bound native auction catalog for the frozen 90US client.

Run with the protected server's CPython/native PVF dependencies available.
The binding hash must be the distributed encrypted Script.pvf SHA256.
"""
from pathlib import Path
from types import SimpleNamespace
import argparse,hashlib,json,os,re,struct,sys

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('pvf',type=Path);ap.add_argument('output',type=Path)
    ap.add_argument('--client-pvf-sha256',required=True)
    ap.add_argument('--server-source',type=Path,default=Path(__file__).resolve().parents[1])
    ap.add_argument('--work-dir',type=Path,default=Path(r'E:\codex\auction-catalog-build'))
    ap.add_argument('--report',type=Path)
    args=ap.parse_args();binding=args.client_pvf_sha256.lower()
    if not re.fullmatch('[0-9a-f]{64}',binding):raise ValueError('Invalid client PVF SHA256')
    args.work_dir.mkdir(parents=True,exist_ok=True)
    os.environ.update(USDOF_SERVER_HOME=str(args.server_source),USDOF_NATIVE_CORE='off',USDOF_DATA_DIR=str(args.work_dir/'data'),
        USDOF_LOG_DIR=str(args.work_dir/'log'),TEMP=str(args.work_dir),TMP=str(args.work_dir))
    sys.path.insert(0,str(args.server_source))
    from pvf.nkpi_reader import PvfReader
    from game.character import item_metadata as im
    from game.native_auction import categories
    rows={}
    with PvfReader.open(args.pvf,memory=True) as reader:
        for kind,mapping in [('stackable',im._load_stackable_map(reader)),('equipment',im._load_equipment_map(reader))]:
            for raw_id,relative in mapping.items():
                iid=int(raw_id)&0xffffffff;path=im._relative_script_path(kind,relative)
                text=reader.read_text(path);name=im._extract_name(text).replace('\0','').strip()
                if not iid or not name:continue
                encoded=name.encode('utf-16le');length=len(encoded)//2
                if length>512:raise ValueError(('Item name exceeds native limit',iid,path))
                meta=SimpleNamespace(item_kind=kind,source_path=path,usable_jobs=im._extract_usable_jobs(text),
                    equipment_type=im._extract_equipment_type(text) if kind=='equipment' else '',
                    stackable_type=im._extract_stackable_type(text) if kind=='stackable' else '')
                rarity=max(0,min(65535,im._extract_rarity(text)));level=max(0,min(65535,im._extract_minimum_level(text)))
                rows[iid]=struct.pack('<IHHHH',iid,rarity,level,categories(meta)[-1],length)+encoded
    if len(rows)>500000:raise ValueError('Native catalog row limit exceeded')
    payload=b''.join(rows[key] for key in sorted(rows))
    data=struct.pack('<8sIII',b'DNFAUC1\0',1,len(rows),len(payload))+binding.encode('ascii')+payload
    args.output.parent.mkdir(parents=True,exist_ok=True)
    temporary=args.output.with_suffix(args.output.suffix+'.tmp')
    with temporary.open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
    os.replace(temporary,args.output)
    report=dict(rows=len(rows),bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),client_pvf_sha256=binding,
        source_pvf_sha256=hashlib.sha256(args.pvf.read_bytes()).hexdigest(),path=str(args.output))
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':main()
