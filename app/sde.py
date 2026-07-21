import sqlite3
import os
from functools import lru_cache

SDE_PATH = os.path.join(os.path.dirname(__file__), "..", "eve.db")

# SDE is static data (EVE's item/blueprint database, not user data) — it never
# changes during the process lifetime, so caching in plain dicts is safe.
# NOTE: these are per-process. With multiple gunicorn workers each worker builds
# its own copy (extra SQLite reads on first use per worker, not shared) — fine at
# this scale, but not a shared cache. Move to Redis if that ever matters.
_name_cache = {}
_category_cache = {}
_materials_cache = {}
_industry_input_cache = {}
_industry_output_cache = {}
_products_cache = {}
_ship_skills_cache = {}
_ship_stats_cache = {}
_module_stats_cache = {}


def clear_caches():
    # Called after eve.db is replaced with a fresh SDE download, so stale
    # names/categories/materials from the old file aren't served forever.
    _name_cache.clear()
    _category_cache.clear()
    _materials_cache.clear()
    _industry_input_cache.clear()
    _industry_output_cache.clear()
    _products_cache.clear()
    _ship_skills_cache.clear()
    _ship_stats_cache.clear()
    _module_stats_cache.clear()
    get_type_name.cache_clear()


@lru_cache(maxsize=None)
def get_type_name(type_id):
    db = sqlite3.connect(SDE_PATH)
    result = db.execute(
        "SELECT typeName FROM invTypes WHERE typeID = ?", (type_id,)
    ).fetchone()
    db.close()
    return result[0] if result else "unknown"


def get_type_names(type_ids):
    # Batched version of get_type_name — only queries SQLite for IDs not already
    # cached, instead of one query per ID like lru_cache would do on first miss.
    type_ids = set(type_ids)
    missing = type_ids - _name_cache.keys()
    if missing:
        db = sqlite3.connect(SDE_PATH)
        placeholders = ",".join("?" * len(missing))
        rows = db.execute(
            f"SELECT typeID, typeName FROM invTypes WHERE typeID IN ({placeholders})",
            list(missing),
        ).fetchall()
        db.close()
        _name_cache.update({row[0]: row[1] for row in rows})
    return {tid: _name_cache[tid] for tid in type_ids if tid in _name_cache}


def get_item_categories(type_ids):
    type_ids = set(type_ids)
    missing = type_ids - _category_cache.keys()
    if missing:
        db = sqlite3.connect(SDE_PATH)
        placeholders = ",".join("?" * len(missing))
        rows = db.execute(
            f"""
            SELECT t.typeID, t.typeName, c.categoryName
            FROM invTypes t
            JOIN invGroups g ON t.groupID = g.groupID
            JOIN invCategories c ON g.categoryID = c.categoryID
            WHERE t.typeID IN ({placeholders})
            """,
            list(missing),
        ).fetchall()
        db.close()
        _category_cache.update({row[0]: {"name": row[1], "category": row[2]} for row in rows})
    return {tid: _category_cache[tid] for tid in type_ids if tid in _category_cache}


def get_industry_input_ids(type_ids):
    # Returns the subset of type_ids that are consumed as an input by ANY
    # industrial activity (manufacturing, invention, reactions, ...). This is
    # what makes "Total Spent" catch e.g. a Rifter bought as the T1 input for
    # Wolf production — category filters alone can't, since "Ship" as a whole
    # category is mostly non-industry purchases.
    type_ids = set(type_ids)
    missing = type_ids - _industry_input_cache.keys()
    if missing:
        db = sqlite3.connect(SDE_PATH)
        placeholders = ",".join("?" * len(missing))
        rows = db.execute(
            f"""
            SELECT DISTINCT materialTypeID
            FROM industryActivityMaterials
            WHERE materialTypeID IN ({placeholders})
            """,
            list(missing),
        ).fetchall()
        db.close()
        found = {row[0] for row in rows}
        # Cache negatives too, so non-input types aren't re-queried every call.
        for tid in missing:
            _industry_input_cache[tid] = tid in found
    return {tid for tid in type_ids if _industry_input_cache.get(tid)}


def get_industry_output_ids(type_ids):
    # Returns the subset of type_ids that some blueprint/formula PRODUCES via
    # manufacturing (activity 1) or reactions (activity 11). Mirror of
    # get_industry_input_ids, used to scope "Total Earned" to industry sales:
    # a manufactured Rifter counts, a sold PLEX doesn't.
    type_ids = set(type_ids)
    missing = type_ids - _industry_output_cache.keys()
    if missing:
        db = sqlite3.connect(SDE_PATH)
        placeholders = ",".join("?" * len(missing))
        rows = db.execute(
            f"""
            SELECT DISTINCT productTypeID
            FROM industryActivityProducts
            WHERE activityID IN (1, 11)
            AND productTypeID IN ({placeholders})
            """,
            list(missing),
        ).fetchall()
        db.close()
        found = {row[0] for row in rows}
        for tid in missing:
            _industry_output_cache[tid] = tid in found
    return {tid for tid in type_ids if _industry_output_cache.get(tid)}


def get_blueprint_products(type_ids):
    # What each blueprint MAKES: {bp_type_id: {"product_id", "qty_per_run"}}.
    # qty_per_run matters — ammo prints produce 100 charges per run, so revenue
    # math that assumes 1 unit/run would be off by 100x for those.
    type_ids = set(type_ids)
    missing = type_ids - _products_cache.keys()
    if missing:
        db = sqlite3.connect(SDE_PATH)
        placeholders = ",".join("?" * len(missing))
        rows = db.execute(
            f"""
            SELECT typeID, productTypeID, quantity
            FROM industryActivityProducts
            WHERE activityID = 1
            AND typeID IN ({placeholders})
            """,
            list(missing),
        ).fetchall()
        db.close()
        for bp_tid, product_id, qty in rows:
            _products_cache[bp_tid] = {"product_id": product_id, "qty_per_run": qty}
        # Negative-cache blueprints with no manufacturing product (research-only
        # or reaction formulas) so they aren't re-queried.
        for tid in missing:
            if tid not in _products_cache:
                _products_cache[tid] = None
    return {tid: _products_cache[tid] for tid in type_ids if _products_cache.get(tid)}


def search_type_ids(name_fragment, limit=500):
    # Case-insensitive substring match on item names, for the transactions
    # page item filter. Not cached — search terms rarely repeat.
    db = sqlite3.connect(SDE_PATH)
    rows = db.execute(
        "SELECT typeID FROM invTypes WHERE typeName LIKE ? LIMIT ?",
        (f"%{name_fragment}%", limit),
    ).fetchall()
    db.close()
    return [row[0] for row in rows]


def get_blueprint_materials(type_ids):
    # activityID = 1 is manufacturing specifically (SDE also has other activity
    # IDs for reactions, invention, etc. that this app doesn't use yet).
    type_ids = set(type_ids)
    missing = type_ids - _materials_cache.keys()
    if missing:
        db = sqlite3.connect(SDE_PATH)
        placeholders = ",".join("?" * len(missing))
        rows = db.execute(
            f"""
            SELECT typeID, materialTypeID, quantity
            FROM industryActivityMaterials
            WHERE activityID = 1
            AND typeID IN ({placeholders})
            """,
            list(missing),
        ).fetchall()
        db.close()
        for type_id, mat_id, qty in rows:
            if type_id not in _materials_cache:
                _materials_cache[type_id] = []
            _materials_cache[type_id].append({"material_id": mat_id, "qty": qty})
        # Blueprints with no manufacturing materials (e.g. reaction-only formulas)
        # still need a cache entry, otherwise they'd be re-queried every call.
        for tid in missing:
            if tid not in _materials_cache:
                _materials_cache[tid] = []
    return {tid: _materials_cache[tid] for tid in type_ids}


def get_ship_skills(ship_type_id):
    # Get required skills for a ship type: (skill_id, skill_name, required_level).
    # Returns list of dicts or empty list if ship has no skill requirements.
    if ship_type_id in _ship_skills_cache:
        return _ship_skills_cache[ship_type_id]

    try:
        db = sqlite3.connect(SDE_PATH)
        rows = db.execute(
            """
            SELECT typeID, skillID, skillLevel
            FROM shipTypeSkillRequirements
            WHERE typeID = ?
            """,
            (ship_type_id,),
        ).fetchall()
        db.close()
    except sqlite3.OperationalError:
        # Table doesn't exist in this SDE version — return empty
        _ship_skills_cache[ship_type_id] = []
        return []

    if not rows:
        _ship_skills_cache[ship_type_id] = []
        return []

    skill_ids = [row[1] for row in rows]
    skill_names = get_type_names(set(skill_ids))

    skills = []
    for ship_id, skill_id, level in rows:
        skills.append({
            "skill_id": skill_id,
            "skill_name": skill_names.get(skill_id, "Unknown"),
            "required_level": level,
        })

    _ship_skills_cache[ship_type_id] = skills
    return skills


def get_ship_stats(ship_type_id):
    # Get CPU and power grid for a ship: {cpu_output, power_output}.
    # Returns dict with 'cpu' and 'power_grid' (or None if not found).
    if ship_type_id in _ship_stats_cache:
        return _ship_stats_cache[ship_type_id]

    try:
        db = sqlite3.connect(SDE_PATH)
        # dgmTypeAttributes has typeID, attributeID, valueInt/valueFloat
        # CPU (48) and Power Grid (11)
        row = db.execute(
            """
            SELECT
                MAX(CASE WHEN attributeID = 48 THEN COALESCE(valueInt, valueFloat) END) as cpu,
                MAX(CASE WHEN attributeID = 11 THEN COALESCE(valueInt, valueFloat) END) as power_grid
            FROM dgmTypeAttributes
            WHERE typeID = ?
            """,
            (ship_type_id,),
        ).fetchone()
        db.close()

        if row and (row[0] or row[1]):
            stats = {"cpu": row[0] or 0, "power_grid": row[1] or 0}
            _ship_stats_cache[ship_type_id] = stats
            return stats
    except sqlite3.OperationalError:
        pass

    _ship_stats_cache[ship_type_id] = None
    return None


def get_module_stats(module_type_id):
    # Get CPU and power grid usage for a module: {cpu_usage, power_usage}.
    # Returns dict with 'cpu' and 'power_grid'.
    if module_type_id in _module_stats_cache:
        return _module_stats_cache[module_type_id]

    # TODO: determine correct attribute IDs for module CPU/PG usage
    # Placeholder: return zeros until we identify the right SDE columns
    stats = {"cpu": 0, "power_grid": 0}
    _module_stats_cache[module_type_id] = stats
    return stats
