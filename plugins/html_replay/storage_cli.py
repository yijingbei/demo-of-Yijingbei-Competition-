"""Replay storage tools; these commands never execute an engine."""
import argparse
import json
from pathlib import Path


def main(argv):
    command=argv[0];parser=argparse.ArgumentParser(prog='replay '+command)
    if command=='viewer':
        parser.add_argument('-o','--output',type=Path,default=Path('replay-viewer.html'))
        parser.add_argument('--force',action='store_true')
        args=parser.parse_args(argv[1:])
        from library import render_viewer
        from exporter import write_html
        write_html(args.output,render_viewer(),args.force)
        print(args.output.resolve());return 0
    parser.add_argument('folder',type=Path)
    if command=='prune':
        parser.add_argument('--keep-last',type=int,default=20)
        parser.add_argument('--apply',action='store_true',help='without this option only list proposed expirations')
    args=parser.parse_args(argv[1:])
    from retention import pin,prune
    if command in {'pin','unpin'}:
        pin(args.folder,enabled=command=='pin')
        print(json.dumps(dict(case=str(args.folder.resolve()),pinned=command=='pin'),ensure_ascii=False))
        return 0
    report=prune(args.folder,args.keep_last,args.apply)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return int(bool(report['errors']))
