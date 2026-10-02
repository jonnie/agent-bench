"""Built-in security-hardening benchmark fixture. Candidate source is data."""

from .base import create_task

TASK = create_task(
    "security-hardening",
    "Harden filesystem-backed note reads",
    """
        Harden solution.notes.read_note(root, name) -> str on POSIX (Linux/macOS).
        root is a trusted existing directory path. name must be a str matching
        ASCII [A-Za-z0-9][A-Za-z0-9_-]{0,63}; all other names raise ValueError.
        Read root / (name + '.txt') as UTF-8, preserving contents/newlines.
        Never follow a final-component symlink, even one pointing within root;
        reject symlinks (including dangling ones) and all non-regular files with
        ValueError. Missing valid names raise FileNotFoundError; invalid UTF-8
        raises UnicodeDecodeError. Protect the actual open against symlink
        substitution rather than only checking resolve()/is_symlink() first:
        use a directory fd, os.open(..., dir_fd=..., O_NOFOLLOW | O_NONBLOCK),
        and fstat the opened descriptor before reading. O_NONBLOCK avoids FIFO
        hangs. Close all descriptors on both success and failure. No writes.
        Public signature remains read_note(root, name). No external dependencies.
        Run public tests with python -m unittest discover -s tests.
        """,
    {
        "notes.py": """
            from pathlib import Path

            def read_note(root, name):
                return (Path(root) / (name + '.txt')).read_text(encoding='utf-8')
        """
    },
    """
        import tempfile
        import unittest
        from pathlib import Path
        from solution.notes import read_note

        class NoteTests(unittest.TestCase):
            def test_normal_note(self):
                with tempfile.TemporaryDirectory() as directory:
                    (Path(directory) / 'welcome.txt').write_text('Hello!\\n', encoding='utf-8')
                    self.assertEqual(read_note(directory, 'welcome'), 'Hello!\\n')

            def test_invalid_name(self):
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(ValueError):
                        read_note(directory, '../private')
        """,
    """
        import errno
        import os
        import random
        import string
        import tempfile
        import unittest
        from pathlib import Path
        from unittest.mock import patch
        from solution import notes

        class NoteContract(unittest.TestCase):
            def setUp(self):
                self.temp = tempfile.TemporaryDirectory()
                self.addCleanup(self.temp.cleanup)
                self.parent = Path(self.temp.name)
                self.root = self.parent / 'notes'
                self.root.mkdir()
                (self.parent / 'secret.txt').write_text('private', encoding='utf-8')
                (self.root / 'valid.txt').write_bytes('café\\r\\nsecond\\n'.encode('utf-8'))

            def test_preserve_unicode_and_newlines(self):
                self.assertEqual(notes.read_note(self.root, 'valid'), 'café\\r\\nsecond\\n')

            def test_valid_names_contents_and_missing(self):
                for name in ['A', 'a_b-9', 'x' * 64]:
                    (self.root / (name + '.txt')).write_text(name, encoding='utf-8')
                    self.assertEqual(notes.read_note(str(self.root), name), name)
                with self.assertRaises(FileNotFoundError):
                    notes.read_note(self.root, 'missing')

            def test_seeded_valid_names_and_contents(self):
                rng = random.Random(7712)
                alphabet = string.ascii_letters + string.digits
                for _ in range(25):
                    name = rng.choice(alphabet) + ''.join(rng.choice(alphabet + '_-')
                                                        for _ in range(rng.randrange(64)))
                    content = ''.join(rng.choice('abcé012') for _ in range(rng.randrange(100)))
                    (self.root / (name + '.txt')).write_text(content, encoding='utf-8')
                    self.assertEqual(notes.read_note(self.root, name), content)

            def test_traversal_absolute_and_non_ascii_rejected(self):
                invalid = ['../secret', str(self.parent / 'secret'), '.', '..', '',
                           'a/b', 'a\\\\b', 'é', 'a.txt', 'a\\n', 'x' * 65,
                           'a\\x00b', None, 5, b'valid', '_prefix', '-prefix']
                for name in invalid:
                    with self.subTest(name=name), self.assertRaises(ValueError):
                        notes.read_note(self.root, name)

            def test_symlinks_inside_outside_and_dangling(self):
                for name, target in [('outside', self.parent / 'secret.txt'),
                                     ('inside', self.root / 'valid.txt'),
                                     ('dangling', self.root / 'missing.txt')]:
                    (self.root / (name + '.txt')).symlink_to(target)
                    with self.subTest(name=name), self.assertRaises(ValueError):
                        notes.read_note(self.root, name)

            def test_directory_and_invalid_utf8(self):
                (self.root / 'directory.txt').mkdir()
                with self.assertRaises(ValueError):
                    notes.read_note(self.root, 'directory')
                (self.root / 'binary.txt').write_bytes(b'\\xff')
                with self.assertRaises(UnicodeDecodeError):
                    notes.read_note(self.root, 'binary')

            def test_open_is_directory_anchored_and_race_safe(self):
                real_open = os.open
                with patch.object(os, 'open', wraps=real_open) as opened:
                    notes.read_note(self.root, 'valid')
                calls = [call for call in opened.call_args_list if call.kwargs.get('dir_fd') is not None]
                self.assertTrue(calls, 'open the note relative to a directory descriptor')
                for call in calls:
                    flags = call.args[1] if len(call.args) > 1 else call.kwargs['flags']
                    self.assertTrue(flags & os.O_NOFOLLOW, 'the actual open must reject symlinks')
                    self.assertTrue(flags & os.O_NONBLOCK, 'the actual open must not block on FIFOs')
                # Do not attempt FIFO reads until nonblocking opens are verified:
                # the deliberately unsafe baseline would otherwise hang.
                os.mkfifo(self.root / 'pipe.txt')
                with self.assertRaises(ValueError):
                    notes.read_note(self.root, 'pipe')

            def test_descriptors_closed_on_success_and_errors(self):
                (self.root / 'directory.txt').mkdir()
                (self.root / 'binary.txt').write_bytes(b'\\xff')
                (self.root / 'link.txt').symlink_to(self.root / 'valid.txt')
                real_open = os.open
                descriptors = []
                def tracked_open(*args, **kwargs):
                    fd = real_open(*args, **kwargs)
                    descriptors.append(fd)
                    return fd
                for name in ['valid', 'directory', 'binary', 'link', 'missing']:
                    descriptors.clear()
                    with patch.object(os, 'open', side_effect=tracked_open):
                        try:
                            notes.read_note(self.root, name)
                        except (ValueError, OSError):
                            pass
                    self.assertTrue(descriptors, 'directory descriptors must be used')
                    for fd in descriptors:
                        with self.assertRaises(OSError) as caught:
                            os.fstat(fd)
                        self.assertEqual(caught.exception.errno, errno.EBADF, 'descriptor leak')
        """,
    {"platform": "posix"},
)
