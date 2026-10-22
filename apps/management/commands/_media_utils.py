import json
import shutil
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.db import models
from django.utils import timezone


MEDIA_FIELD_TYPES = (models.FileField, models.ImageField)


@dataclass
class MediaReference:
    model: type
    obj: object
    field: models.Field
    name: str
    relative_path: Path
    absolute_path: Path


def media_root():
    return Path(settings.MEDIA_ROOT).resolve()


def normalize_text(value):
    text = unicodedata.normalize('NFKD', str(value))
    text = ''.join(char for char in text if not unicodedata.combining(char))
    return text.casefold()


def normalize_compact(value):
    text = normalize_text(value)
    return ''.join(char for char in text if char.isalnum())


def iter_media_fields():
    for model in apps.get_models():
        for field in model._meta.get_fields():
            if isinstance(field, MEDIA_FIELD_TYPES):
                yield model, field


def iter_references():
    root = media_root()
    for model, field in iter_media_fields():
        queryset = model.objects.exclude(**{field.name: ''}).exclude(**{f'{field.name}__isnull': True})
        for obj in queryset:
            value = getattr(obj, field.name)
            if not value:
                continue
            name = value.name
            if not name:
                continue
            relative_path = Path(name)
            yield MediaReference(
                model=model,
                obj=obj,
                field=field,
                name=name,
                relative_path=relative_path,
                absolute_path=(root / relative_path).resolve(),
            )


def relative_media_path(path):
    root = media_root()
    return path.resolve().relative_to(root).as_posix()


def collect_media_files():
    root = media_root()
    if not root.exists():
        return []
    return [path.resolve() for path in root.rglob('*') if path.is_file()]


def collect_reference_names():
    return {ref.relative_path.as_posix() for ref in iter_references()}


def expected_upload_dir(field):
    upload_to = field.upload_to
    if callable(upload_to):
        return ''
    return str(upload_to or '').strip('/\\')


def target_relative_path(ref, candidate):
    folder = expected_upload_dir(ref.field)
    if not folder:
        return relative_media_path(candidate)
    return (Path(folder) / candidate.name).as_posix()


def unique_target_path(relative_path):
    root = media_root()
    candidate = root / relative_path
    if not candidate.exists():
        return candidate

    parent = candidate.parent
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        next_candidate = parent / f'{stem}_{counter}{suffix}'
        if not next_candidate.exists():
            return next_candidate
        counter += 1


def candidate_score(ref, candidate):
    expected_name = ref.relative_path.name
    candidate_name = candidate.name
    expected_compact = normalize_compact(expected_name)
    candidate_compact = normalize_compact(candidate_name)

    if candidate.suffix.casefold() != ref.relative_path.suffix.casefold():
        return 0
    if expected_compact == candidate_compact:
        return 100
    if expected_compact and (expected_compact in candidate_compact or candidate_compact in expected_compact):
        return 92

    expected_stem = normalize_compact(ref.relative_path.stem)
    candidate_stem = normalize_compact(candidate.stem)
    if expected_stem and (expected_stem in candidate_stem or candidate_stem in expected_stem):
        return 88

    ratio = SequenceMatcher(None, expected_compact, candidate_compact).ratio()
    return int(ratio * 100)


def find_best_candidate(ref, files):
    best = None
    best_score = 0
    for candidate in files:
        score = candidate_score(ref, candidate)
        if score > best_score:
            best = candidate
            best_score = score
    if best and best_score >= 76:
        return best, best_score
    return None, best_score


def create_backup():
    root = Path(settings.BASE_DIR).resolve()
    backup_dir = root / 'backups' / 'media'
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = timezone.now().strftime('%Y%m%d_%H%M%S')

    db_path = Path(settings.DATABASES['default']['NAME']).resolve()
    db_backup = None
    if db_path.exists():
        db_backup = backup_dir / f'db_{timestamp}.sqlite3'
        shutil.copy2(db_path, db_backup)

    report_path = backup_dir / f'relatorio_{timestamp}.json'
    return backup_dir, db_backup, report_path


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
