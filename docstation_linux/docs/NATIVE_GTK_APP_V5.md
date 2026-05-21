# DocStation Linux v5

Исправлен запуск нативного GTK-приложения.

Причина v4: в окне входа метод `set_role()` вызывался до создания поля `self.password`, поэтому запуск падал с `AttributeError: LoginWindow object has no attribute password`.

Изменение: роль оператора выбирается после создания поля пароля.
