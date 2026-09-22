#!/usr/bin/env python3
"""Stage pinned Skill sources for website rendering, without changing the Skill."""
import argparse
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server import matrix_template_api as api
from server import matrix_motion_v3 as v3


def stage(skill, output):
    skill, output = Path(skill).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("Use a new staging directory")
    output.mkdir(parents=True)
    for identifier in v3.IDS:
        source = skill / 'assets/templates' / identifier
        if any(p.is_symlink() for p in source.rglob('*')):
            raise ValueError('Linked template resources are not accepted')
        root = output / identifier
        shutil.copytree(source, root)
        config = api.FIXED_SKILL_TEMPLATE_CONFIGS[identifier]
        original = json.loads((root/'template.json').read_text(encoding='utf-8'))
        (root/'upstream-template.json').write_text(json.dumps(original,ensure_ascii=False,indent=2),encoding='utf-8')
        fields = ('title','subtitle','body','cta')
        schema = html.escape(json.dumps([{'id':f,'type':'string','default':''} for f in fields]),quote=True)
        if identifier == v3.BILINGUAL:
            fonts = root/'assets/fonts'; fonts.mkdir(parents=True,exist_ok=True)
            for name in ('MaShanZheng-Regular.ttf','NotoSerifSC-Variable.ttf','OFL-MaShanZheng.txt','OFL-NotoSerifSC.txt'):
                shutil.copy2(skill/'assets/fonts'/name,fonts/name)
            markup = f'<html data-composition-variables="{schema}"><head></head><body><main id="root" data-composition-id="main" data-duration="8" data-width="1080" data-height="1920">'
            markup += ''.join(f'<div data-var-text="{f}"></div>' for f in fields) + '</main></body></html>'
        else:
            markup = (root/'index.html').read_text(encoding='utf-8')
            markup, count = re.subn(r'data-composition-variables="[^"]*"',f'data-composition-variables="{schema}"',markup,count=1)
            if count != 1:
                raise ValueError('Template variables changed')
            markup = re.sub(r'\sdata-var-src="[^"]*"','',markup)
            if identifier == v3.INSET:
                markup = markup.replace('id="inset-frame"','id="inset-frame" data-matrix-gpu-overlay="1"')
                markup = markup.replace('id="media-stage"','id="media-stage" data-matrix-gpu-overlay-host="1"')
            if identifier == v3.OPENING:
                # Website policy preserves native source colors, including HDR.
                markup, count = re.subn(r'\sdata-color-grading="[^"]*"','',markup)
                if count != 5:
                    raise ValueError('Fixed-opening grading declarations changed')
            if hashlib.sha256((root/config['bgm_path']).read_bytes()).hexdigest() != config['bgm_sha256']:
                raise ValueError('Bound audio hash changed')
        (root/'index.html').write_text(markup,encoding='utf-8')
        manifest = dict(id=identifier,version=1,renderer='hyperframes',width=1080,height=1920,fps=30,
                        frames=config['frames'],duration=config['duration'],
                        media={'requiredDistinctVideos':config['required_visuals'],
                               'minimumPreparedDuration':config['slot_frames'][0]/30,
                               'paths':list(config['media_paths'])},
                        upstream_commit=v3.UPSTREAM_COMMIT,
                        website_color_policy='native-source; only inset opening uses its authored desaturated backplate')
        if config['bgm_path']:
            manifest['referenceAudio']=dict(path=config['bgm_path'],sha256=config['bgm_sha256'],duration=config['bgm_duration'])
        (root/'template.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        scripts={key:f'npx hyperframes@{v3.VERSION} {cmd}' for key,cmd in
                 [('dev','preview'),('check','check'),('render','render'),('publish','publish')]}
        (root/'package.json').write_text(json.dumps({'private':True,'scripts':scripts}),encoding='utf-8')
    return output


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--skill',required=True);p.add_argument('--output',required=True)
    args=p.parse_args();print(stage(args.skill,args.output))
