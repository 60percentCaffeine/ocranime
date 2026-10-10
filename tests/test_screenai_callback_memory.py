"""Exercise model callbacks on Screen AI's real native worker stacks."""

import subprocess
import sys
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform == 'linux', 'requires Linux allocator accounting')
class ScreenAICallbackMemoryTests(unittest.TestCase):
    def test_model_reload_does_not_retain_a_new_python_copy_of_weights(self):
        library = Path.home() / '.config/screen_ai/resources/libchromescreenai.so'
        if not library.exists():
            self.skipTest('local Screen AI models are unavailable')
        script = r'''
import ctypes
import gc
import json
import ocranime

class Mallinfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_size_t) for name in (
        'arena', 'ordblks', 'smblks', 'hblks', 'hblkhd', 'usmblks',
        'fsmblks', 'uordblks', 'fordblks', 'keepcost')]
libc = ctypes.CDLL(None)
if not hasattr(libc, 'mallinfo2'):
    raise SystemExit(77)
libc.mallinfo2.restype = Mallinfo
def allocated():
    gc.collect()
    info = libc.mallinfo2()
    return info.uordblks + info.hblkhd
engine = ocranime._load_screenai()
engine['lib'].UninitializeOCR()
before = allocated()
for _ in range(20):
    assert engine['lib'].InitOCRUsingCallback()
    engine['lib'].UninitializeOCR()
growth = allocated() - before
print(json.dumps({'growth_bytes': growth}), flush=True)
assert growth < 8 * 1024**2, f'model callbacks retained {growth / 1024**2:.1f} MiB'
'''
        result = subprocess.run(
            [sys.executable, '-c', script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 77:
            self.skipTest('glibc mallinfo2 is unavailable')
        self.assertEqual(result.returncode, 0,
                         result.stdout + '\n' + result.stderr[-2000:])


if __name__ == '__main__':
    unittest.main()
