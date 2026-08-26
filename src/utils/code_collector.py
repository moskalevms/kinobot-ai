import os
import glob
from pathlib import Path


class CodeCollector:
    def __init__(self, source_dir=None, output_file=None):
        """
        Инициализация сборщика кода

        Args:
            source_dir (str): Исходная папка с кодом (по умолчанию родительская папка от скрипта)
            output_file (str): Путь к выходному файлу (по умолчанию рабочий стол)
        """
        # Определяем путь относительно расположения скрипта
        script_dir = Path(__file__).parent
        if source_dir is None:
            self.source_dir = script_dir.parent  # Поднимаемся на уровень выше из utils
        else:
            self.source_dir = Path(source_dir)

        if output_file is None:
            # Путь к рабочему столу
            desktop = Path.home() / "Desktop"
            self.output_file = desktop / "код.txt"
        else:
            self.output_file = Path(output_file)

        # Обновленный список расширений: добавлены .html и .yml (для docker-compose.yml)
        self.allowed_extensions = {'.py', '.txt', '.html', '.yml'}

        # Специфические файлы без стандартных расширений
        self.specific_files = ['Dockerfile']

        print(f"Исходная директория: {self.source_dir}")
        print(f"Выходной файл: {self.output_file}")

    def collect_files(self):
        """Сбор всех файлов .py, .txt, .html, .yml и Dockerfile из исходной директории"""
        files = []

        # Проверяем существование исходной директории
        if not self.source_dir.exists():
            print(f"ОШИБКА: Папка {self.source_dir} не существует!")
            return files

        # Рекурсивный поиск по расширениям
        for ext in self.allowed_extensions:
            pattern = str(self.source_dir / "**" / f"*{ext}")
            found_files = glob.glob(pattern, recursive=True)
            files.extend(found_files)
            print(f"Найдено {len(found_files)} файлов {ext}")

        # Поиск специфических файлов (например, Dockerfile без расширения)
        for filename in self.specific_files:
            pattern = str(self.source_dir / "**" / filename)
            found_files = glob.glob(pattern, recursive=True)
            files.extend(found_files)
            print(f"Найдено {len(found_files)} файлов '{filename}'")

        # Удаляем дубликаты (на случай пересечений) и сортируем файлы для удобства чтения
        files = sorted(list(set(files)))
        return files

    def read_file_content(self, file_path):
        """Чтение содержимого файла с обработкой кодировки"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except UnicodeDecodeError:
            # Если UTF-8 не работает, пробуем другие кодировки
            try:
                with open(file_path, 'r', encoding='cp1251') as f:
                    return f.read()
            except:
                return f"# Ошибка чтения файла: {file_path}"
        except Exception as e:
            return f"# Ошибка при чтении файла {file_path}: {str(e)}"

    def write_collected_code(self):
        """Запись собранного кода в выходной файл"""
        files = self.collect_files()

        if not files:
            print("Файлы не найдены! Проверьте путь к исходной папке.")
            return 0

        print(f"Найдено файлов для обработки: {len(files)}")

        try:
            with open(self.output_file, 'w', encoding='utf-8') as output:
                for file_path in files:
                    # Получаем относительный путь от корневой папки проекта
                    try:
                        relative_path = Path(file_path).relative_to(self.source_dir)
                    except ValueError:
                        # Если файл находится вне source_dir, используем абсолютный путь
                        relative_path = Path(file_path)

                    print(f"Обрабатывается: {relative_path}")

                    # Заголовок файла
                    output.write(f"[file name]: {relative_path}\n")
                    output.write("[file content begin]\n")

                    # Содержимое файла
                    content = self.read_file_content(file_path)
                    output.write(content)

                    # Если файл не заканчивается переносом строки, добавляем его
                    if content and not content.endswith('\n'):
                        output.write('\n')

                    output.write("[file content end]\n\n")

            print(f"✅ Код успешно записан в: {self.output_file}")
            return len(files)

        except Exception as e:
            print(f"❌ Ошибка при записи файла: {e}")
            return 0

    def get_statistics(self):
        """Получение статистики по собранным файлам"""
        files = self.collect_files()
        stats = {
            'total_files': len(files),
            'py_files': len([f for f in files if f.endswith('.py')]),
            'txt_files': len([f for f in files if f.endswith('.txt')]),
            'html_files': len([f for f in files if f.endswith('.html')]),
            'yml_files': len([f for f in files if f.endswith('.yml')]),
            'dockerfile_files': len([f for f in files if Path(f).name == 'Dockerfile']),
            'files_list': [str(Path(f).relative_to(self.source_dir)) for f in files]
        }
        return stats


def collect_src_code():
    """Простая функция для сбора кода из папки src (доработана для .html, .yml и Dockerfile)"""
    # Определяем путь относительно этого скрипта
    script_dir = Path(__file__).parent
    src_dir = script_dir.parent  # Поднимаемся на уровень выше из utils в src

    print(f"Поиск файлов в: {src_dir}")

    # Находим все файлы по расширениям
    py_files = glob.glob(str(src_dir / "**" / "*.py"), recursive=True)
    txt_files = glob.glob(str(src_dir / "**" / "*.txt"), recursive=True)
    html_files = glob.glob(str(src_dir / "**" / "*.html"), recursive=True)
    yml_files = glob.glob(str(src_dir / "**" / "*.yml"), recursive=True)

    # Специфический поиск Dockerfile
    dockerfiles = glob.glob(str(src_dir / "**" / "Dockerfile"), recursive=True)

    all_files = sorted(py_files + txt_files + html_files + yml_files + dockerfiles)

    print(f"Найдено .py файлов: {len(py_files)}")
    print(f"Найдено .txt файлов: {len(txt_files)}")
    print(f"Найдено .html файлов: {len(html_files)}")
    print(f"Найдено .yml файлов: {len(yml_files)}")
    print(f"Найдено Dockerfile: {len(dockerfiles)}")
    print(f"Всего файлов: {len(all_files)}")

    if not all_files:
        print("❌ Файлы не найдены! Проверьте путь.")
        return

    output_file = Path.home() / "Desktop" / "код.txt"

    try:
        with open(output_file, 'w', encoding='utf-8') as out:
            for file_path in all_files:
                # Получаем относительный путь от папки src
                try:
                    relative_path = Path(file_path).relative_to(src_dir)
                except ValueError:
                    relative_path = Path(file_path)

                print(f"Добавляем: {relative_path}")

                out.write(f"[file name]: {relative_path}\n")
                out.write("[file content begin]\n")

                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        out.write(content)
                        if content and not content.endswith('\n'):
                            out.write('\n')
                except Exception as e:
                    error_msg = f"# Ошибка чтения файла: {e}\n"
                    out.write(error_msg)
                    print(f"Ошибка чтения {file_path}: {e}")

                out.write("[file content end]\n\n")

        print(f"✅ Код записан в: {output_file}")

    except Exception as e:
        print(f"❌ Ошибка при записи: {e}")


# Быстрый запуск
if __name__ == "__main__":
    print("=== Запуск сборщика кода ===")

    # Способ 1: Использование класса
    collector = CodeCollector()
    stats = collector.get_statistics()
    print(f"Статистика: {stats['py_files']} .py, {stats['txt_files']} .txt, "
          f"{stats['html_files']} .html, {stats['yml_files']} .yml, "
          f"{stats['dockerfile_files']} Dockerfile файлов")

    file_count = collector.write_collected_code()
    print(f"Обработано файлов: {file_count}")

    # Способ 2: Простая функция (если нужно)
    # collect_src_code()op
