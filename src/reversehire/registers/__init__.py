"""Regional register: free official company lists (RNA state aid, GLEIF LEI register,
OpenCoesione, AGCM legality rating) downloaded, merged by tax code into a local,
user-editable SQLite file, and used as a discovery source.

Public API
    RegisterManager(folder=None, settings=None)          folder default <data>/registers
        .sources_info() / .attributions()
        .estimate(ids) / .check_updates(ids) / .download(ids, progress, cancel)
        .refresh(sources=None, regions=None, progress=None, cancel=None, download=True)
        .import_bought_list(path, mapping=None)           Registro Imprese list bought by the user
        .reminder() / .disk_usage() / .purge_raw(ids)
        .db -> RegisterDB
    RegisterDB
        .find(region, province, municipality, ateco_prefixes, text, only_active, limit) -> [RegisterCompany]
        .count(...) / .get(key) / .provenance_of(key) / .stats() / .coverage_note(province, division)
        .edit(key, {field: value}) / .unlock(key, field) / .add_company(fields, cf)
        .hide(key) / .unhide(key) / .delete(key) / .undo() / .can_undo() / .possible_duplicates(key)
        .export_csv(path) / .import_csv(path)
    RegisterCompany.to_candidate() -> models.Company (as the discovery sources yield)
    RegionalRegisterSource(manager, region, store=None)   discovery source "regional_register"
    RegisterJob(manager.refresh, ...)                      worker thread with cancel and progress
    RegisterProgress                                       progress events (done/estimated bytes and rows)
    shared_manager(settings=None, folder=None)            the one manager of a data folder
    lists.import_list / refresh_list / delete_list / lists the user's imported CSV/XLSX lists,
                                                           each a source "list:<id>" of the register
    lists.migrate_imported_lists(store, db)                one-time, idempotent move of the lists that
                                                           older versions kept in the Store
"""

from __future__ import annotations

from .base import Estimate, RegisterSource, UpdateCheck
from .db import OFFICIAL_COUNTS, ImportCsvReport, LoadReport, RegisterDB
from .download import DownloadResult, FileResult, RemoteFile
from .manager import (SOURCE_CLASSES, RefreshReport, RegisterJob, RegisterManager, SourceRefresh, close_shared,
                      shared_manager)
from .model import (AGCM, DOWNLOADABLE, GLEIF, OPENCOESIONE, RI_BOUGHT, RNA, RegisterProgress, RegisterRow,
                    Signal)
from .record import DISCOVERY_SOURCE, RegisterCompany


# The submodule is loaded here, BEFORE the function of the same name is defined: loading
# a submodule binds its name on the package, which would otherwise replace the function.
from . import discovery_source as _discovery_source_module  # noqa: E402,F401


def discovery_source(manager: RegisterManager, region=None, store=None):
    """The discovery source object. With `store`, the lists an older version kept there
    are moved into the register before the first search (once)."""
    return _discovery_source_module.RegionalRegisterSource(manager, region, store=store)


__all__ = [
    "AGCM", "DISCOVERY_SOURCE", "DOWNLOADABLE", "DownloadResult", "Estimate", "FileResult", "GLEIF",
    "ImportCsvReport", "LoadReport", "OFFICIAL_COUNTS", "OPENCOESIONE", "RI_BOUGHT", "RNA", "RefreshReport",
    "RegisterCompany", "RegisterDB", "RegisterJob", "RegisterManager", "RegisterProgress", "RegisterRow",
    "RegisterSource", "RemoteFile", "SOURCE_CLASSES", "Signal", "SourceRefresh", "UpdateCheck", "close_shared",
    "discovery_source", "shared_manager",
]
