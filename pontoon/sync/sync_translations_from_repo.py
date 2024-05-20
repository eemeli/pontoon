import logging
from datetime import datetime
from os.path import join, relpath, splitext
from typing import cast

from django.db.models import Q
from moz.l10n.paths import L10nConfigPaths, L10nDiscoverPaths, parse_android_locale
from moz.l10n.resource import bilingual_extensions

from pontoon.actionlog.models import ActionLog
from pontoon.base.models import Locale, Project, TranslatedResource, Translation, User
from pontoon.base.models.changed_entity_locale import ChangedEntityLocale
from pontoon.base.models.entity import Entity
from pontoon.base.models.resource import Resource
from pontoon.sync.checkout import Checkout
from pontoon.sync.formats import parse
from pontoon.sync.vcs.translation import VCSTranslation

log = logging.getLogger(__name__)


def sync_translations_from_repo(
    project: Project,
    locales: dict[str, Locale],
    checkouts: list[Checkout],
    ref_checkout: Checkout,
    paths: L10nConfigPaths | L10nDiscoverPaths,
    now: datetime,
) -> None:
    delete_removed_translated_resources(project, checkouts, ref_checkout, paths)
    repo_translations = find_updates_from_repo_to_db(
        project, locales, checkouts, ref_checkout, paths
    )
    if repo_translations:
        update_db_translations(repo_translations, now)


def delete_removed_translated_resources(
    project: Project,
    checkouts: list[Checkout],
    ref_checkout: Checkout,
    paths: L10nConfigPaths | L10nDiscoverPaths,
) -> int:
    del_q = Q()
    for co in checkouts:
        if co != ref_checkout:
            for path in co.removed:
                _, ext = splitext(path)
                if ext in bilingual_extensions:
                    ref = paths.find_reference(join(co.path, path))
                    if ref:
                        ref_path, path_vars = ref
                        locale_code = get_path_locale(path_vars)
                        if locale_code is not None:
                            db_path = relpath(ref_path, paths.ref_root)
                            if db_path.endswith(".pot"):
                                db_path = db_path[:-1]
                            del_q |= Q(resource__path=db_path, locale__code=locale_code)
    if del_q:
        count, _ = (
            TranslatedResource.objects.filter(resource__project=project)
            .filter(del_q)
            .delete()
        )
        return count
    return 0


def find_updates_from_repo_to_db(
    project: Project,
    locales: dict[str, Locale],
    checkouts: list[Checkout],
    ref_checkout: Checkout,
    paths: L10nConfigPaths | L10nDiscoverPaths,
) -> dict[tuple[int, int], tuple[dict[int | None, str], bool]] | None:
    """
    `(entity.id, locale.id) -> (plural_form -> string, fuzzy)`

    Translations in changed resources, excluding:
    - Exact matches with previous approved or pretranslated translations
    - Entity/Locale combos for which Pontoon has changes since the last sync
    - Translations for which no matching entity is found
    """
    resource_paths: set[str] = set()
    # {db_path, locale.id}
    translated_resources: set[tuple[str, int]] = set()
    # (db_path, tx.key, locale.id) -> (plural_form -> string, fuzzy)
    translations: dict[tuple[str, str, int], tuple[dict[int | None, str], bool]] = {}
    for co in checkouts:
        if co != ref_checkout:
            for path in co.changed:
                target_path = join(co.path, path)
                ref = paths.find_reference(target_path)
                if ref:
                    ref_path, path_vars = ref
                    locale_code = get_path_locale(path_vars)
                    locale: Locale | None = locales.get(locale_code, None)
                    if locale is not None:
                        try:
                            res = parse(target_path, ref_path, locale)
                        except Exception as error:
                            log.error(
                                f"Skipping translated resource {path} due to: {error}"
                            )
                            continue
                        db_path = relpath(ref_path, paths.ref_root)
                        if db_path.endswith(".pot"):
                            db_path = db_path[:-1]
                        resource_paths.add(db_path)
                        translated_resources.add((db_path, locale.id))
                        translations.update(
                            ((db_path, tx.key, locale.id), (tx.strings, tx.fuzzy))
                            for tx in cast(list[VCSTranslation], res.translations)
                        )
    if not translations:
        return None

    resources: dict[str, Resource] = {
        res.path: res
        for res in Resource.objects.filter(project=project, path__in=resource_paths)
    }

    # Exclude translations for which DB & repo already match
    # TODO: Should be able to use repo diff to identify changed entities and refactor this.
    tr_q = Q()
    for db_path, locale_id in translated_resources:
        res = resources.get(db_path, None)
        if res is not None:
            tr_q |= Q(entity__resource=res, locale_id=locale_id)
    if tr_q:
        for tx in (
            Translation.objects.filter(tr_q)
            .filter(Q(approved=True) | Q(pretranslated=True))
            .values(
                "entity__resource__path",
                "entity__key",
                "entity__string",  # terminology/common and tutorial/playground use string instead of key.
                "locale_id",
                "plural_form",
                "string",
            )
        ):
            key = (
                tx["entity__resource__path"],
                tx["entity__key"] or tx["entity__string"],
                tx["locale_id"],
            )
            mod_tx = translations.get(key, None)
            if mod_tx is not None:
                plural_form = tx["plural_form"]
                strings, _ = mod_tx
                if strings.get(plural_form, None) == tx["string"]:
                    if len(strings) > 1:
                        del strings[plural_form]
                    else:
                        del translations[key]
    if not translations:
        return None

    # TODO: Pass DB changes in as an argument?
    # If repo and database both have changes, database wins.
    for change in ChangedEntityLocale.objects.filter(
        entity__resource__project=project
    ).values("entity__key", "entity__string", "entity__resource__path", "locale_id"):
        key = (
            change["entity__resource__path"],
            change["entity__key"] or change["entity__string"],
            change["locale_id"],
        )
        if key in translations:
            del translations[key]
    if not translations:
        return None

    entity_q = Q()
    for db_path, ent_key, _ in translations:
        entity_q |= Q(resource=resources[db_path]) & (
            Q(key=ent_key) | Q(key="", string=ent_key)
        )
    entities: dict[tuple[str, str], int] = {
        (e["resource__path"], e["key"] or e["string"]): e["id"]
        for e in Entity.objects.filter(entity_q).values(
            "id", "key", "string", "resource__path"
        )
    }
    # (entity.id, locale.id) -> (plural_form -> string, fuzzy)
    res: dict[tuple[int, int], tuple[dict[int | None, str], bool]] = {}
    """ foo """
    for (db_path, ent_key, locale_id), tx in translations.items():
        entity_id = entities.get((db_path, ent_key), None)
        if entity_id is not None:
            res[(entity_id, locale_id)] = tx
    return res


def update_db_translations(
    repo_translations: dict[tuple[int, int], tuple[dict[int | None, str], bool]],
    now: datetime,
) -> None:
    sync_user = User.objects.get(username="pontoon-sync")
    translations_to_reject = Q()
    actions: list[ActionLog] = []

    # Approve matching suggestions
    matching_suggestions_q = Q()
    for (entity_id, locale_id), (strings, _) in repo_translations.items():
        for plural_form, string in strings.items():
            matching_suggestions_q |= Q(
                entity_id=entity_id,
                locale_id=locale_id,
                plural_form=plural_form,
                string=string,
            )
    suggestions = list(
        Translation.objects.filter(matching_suggestions_q).filter(
            approved=False, pretranslated=False
        )
    )
    dirty_fields: set[str] = set()
    approve_count = 0
    for tx in suggestions:
        key = (tx.entity_id, tx.locale_id)
        _, fuzzy = repo_translations[key]
        del repo_translations[key]

        if tx.rejected:
            tx.rejected = False
            tx.unrejected_user = None
            tx.unrejected_date = now
            actions.append(
                ActionLog(
                    action_type=ActionLog.ActionType.TRANSLATION_UNREJECTED,
                    performed_by=sync_user,
                    translation=tx,
                )
            )

        tx.active = True
        tx.fuzzy = fuzzy
        if not fuzzy:
            tx.approved = True
            tx.approved_user = None
            tx.approved_date = now
            tx.pretranslated = False
            tx.unapproved_user = None
            tx.unapproved_date = None
            translations_to_reject |= Q(
                entity=tx.entity, locale=tx.locale, plural_form=tx.plural_form
            ) & ~Q(id=tx.id)
            actions.append(
                ActionLog(
                    action_type=ActionLog.ActionType.TRANSLATION_APPROVED,
                    performed_by=sync_user,
                    translation=tx,
                )
            )
            approve_count += 1
        dirty_fields.update(tx.get_dirty_fields())
    update_count = Translation.objects.bulk_update(suggestions, list(dirty_fields))
    if update_count:
        count = (
            str(approve_count)
            if approve_count == update_count
            else f"{approve_count}/{update_count}"
        )
        log.info(f"Approved {count} translation(s) from repo changes")

    if repo_translations:
        # Add new approved translations for the remainder
        new_translations: list[Translation] = []
        for (entity_id, locale_id), (strings, fuzzy) in repo_translations.items():
            for plural_form, string in strings.items():
                # Note: no tx.entity.resource, which would be required by tx.save()
                tx = Translation(
                    entity=Entity(entity_id),
                    locale=Locale(locale_id),
                    string=string,
                    plural_form=plural_form,
                    date=now,
                    active=True,
                )
                if fuzzy:
                    tx.fuzzy = True
                else:
                    tx.approved = True
                    tx.approved_date = now
                new_translations.append(tx)
                actions.append(
                    ActionLog(
                        action_type=ActionLog.ActionType.TRANSLATION_CREATED,
                        created_at=now,
                        performed_by=sync_user,
                        translation=tx,
                    )
                )
        created = Translation.objects.bulk_create(new_translations)
        for tx in created:
            translations_to_reject |= Q(
                entity_id=tx.entity_id,
                locale_id=tx.locale_id,
                plural_form=tx.plural_form,
            ) & ~Q(id=tx.id)
        if created:
            log.info(f"Created {len(created)} translation(s) from repo changes")

    if translations_to_reject:
        rejected = Translation.objects.filter(rejected=False).filter(
            translations_to_reject
        )
        actions.extend(
            ActionLog(
                action_type=ActionLog.ActionType.TRANSLATION_REJECTED,
                performed_by=sync_user,
                translation=tx,
            )
            for tx in rejected
        )
        reject_count = rejected.update(
            active=False,
            approved=False,
            approved_user=None,
            approved_date=None,
            rejected=True,
            rejected_user=None,
            rejected_date=now,
            pretranslated=False,
            fuzzy=False,
        )
        if reject_count:
            log.info(f"Rejected {reject_count} translation(s) from repo changes")

    if actions:
        ActionLog.objects.bulk_create(actions)


def get_path_locale(path_vars: dict[str, str]) -> str | None:
    if "locale" in path_vars:
        return path_vars["locale"]
    elif "android_locale" in path_vars:
        return parse_android_locale(path_vars["android_locale"])
    else:
        return None
