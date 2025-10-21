from app import app
with app.app_context():
    for r in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        print(f"{','.join(sorted(r.methods))}  {r.endpoint:25s} -> {r.rule}")
