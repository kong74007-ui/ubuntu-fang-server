"""Check HDR encoding and PNG transfer tags before switching a worker release."""
import json
import subprocess
import tempfile
from pathlib import Path


def run(command):
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)


def probe(path):
    return json.loads(run([
        'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_streams',
        '-of', 'json', str(path),
    ]).stdout)['streams'][0]


def main():
    with tempfile.TemporaryDirectory(prefix='matrix-hdr-check-') as temporary:
        root = Path(temporary)
        for transfer, code in [('arib-std-b67', 18), ('smpte2084', 16)]:
            video, image = root / 'hdr.mp4', root / 'hdr.png'
            run([
                'ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                'testsrc2=s=64x64:r=30:d=0.1', '-c:v', 'libx265',
                '-preset', 'ultrafast', '-pix_fmt', 'yuv420p10le',
                '-x265-params', f'pools=1:log-level=error:colorprim=9:colormatrix=9:transfer={code}',
                '-color_primaries', 'bt2020', '-color_trc', transfer,
                '-colorspace', 'bt2020nc', '-color_range', 'tv', str(video),
            ])
            run([
                'ffmpeg', '-v', 'error', '-y', '-i', str(video), '-frames:v', '1',
                '-pix_fmt', 'rgb48be', '-color_primaries', 'bt2020',
                '-color_trc', transfer, '-colorspace', 'rgb', str(image),
            ])
            for path, pixel in [(video, 'yuv420p10le'), (image, 'rgb48be')]:
                info = probe(path)
                if (info.get('pix_fmt') != pixel or info.get('color_transfer') != transfer
                        or info.get('color_primaries') != 'bt2020'):
                    raise RuntimeError('FFmpeg must preserve ten-bit HDR and 16-bit PNG cICP tags')
    print('HDR runtime check passed: HEVC Main 10 HLG/PQ and HDR PNG')


if __name__ == '__main__':
    main()
