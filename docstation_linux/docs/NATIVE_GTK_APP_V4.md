# DocStation Linux v4 — GTK startup fix

Исправлен запуск нативного GTK-приложения на Ubuntu, где установлен GTK4.

Проблема v3:

```text
ImportError: Requiring namespace 'Gdk' version '3.0', but '4.0' is already loaded
```

Причина: приложение написано под GTK3, но версия namespace `Gdk` не была зафиксирована до импорта.

Исправление:

```python
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
```

Также установщик явно ставит `gir1.2-pango-1.0`.
