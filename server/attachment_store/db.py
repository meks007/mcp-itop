# This file has been deleted as part of the db-layer refactor.
#
# Everything that was here has moved:
#
#   IMAGE_STORE_TTL_SECONDS       -> config.py (env: IMAGE_STORE_TTL_SECONDS)
#   INLINE_IMAGE_REF_TTL          -> config.py (was already there)
#   SQLITE_DB_PATH / path config  -> db/sqlite.py  (env: SQLITE_DB_PATH)
#   Vacuum thread + loop          -> db/sqlite.py  (Backend.connect())
#   DDL / CREATE TABLE            -> attachment_store/session.py
#                                    attachment_store/refs.py
#                                    (via db.register_schema())
#   init_db()                     -> db.init()
#
# Callers: replace `attachment_store.init_db()` with `db.init()`.
# Schema registration is automatic when attachment_store is imported.
