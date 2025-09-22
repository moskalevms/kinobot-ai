import tkinter as tk
from tkinter import ttk, scrolledtext
from agent import MovieAgent


class MovieBotGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Kinobot AI - Подбор фильмов")
        self.root.geometry("700x600")

        # Переменная для режима работы
        self.use_api = tk.BooleanVar(value=True)

        # Инициализируем агента
        self.agent = MovieAgent(use_api=self.use_api.get())

        self.create_widgets()

    def create_widgets(self):
        # Основной фрейм
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Заголовок
        title_label = ttk.Label(main_frame, text="Kinobot AI", font=("Arial", 16, "bold"))
        title_label.grid(row=0, column=0, columnspan=2, pady=(0, 10))

        # Подзаголовок
        subtitle_label = ttk.Label(main_frame, text="Ваш персональный помощник по подбору фильмов", font=("Arial", 10))
        subtitle_label.grid(row=1, column=0, columnspan=2, pady=(0, 10))

        # Переключатель режима
        mode_frame = ttk.Frame(main_frame)
        mode_frame.grid(row=2, column=0, columnspan=2, pady=(0, 10))

        ttk.Label(mode_frame, text="Режим работы:").pack(side=tk.LEFT)
        ttk.Radiobutton(mode_frame, text="API", variable=self.use_api, value=True, command=self.update_mode).pack(
            side=tk.LEFT, padx=5)
        ttk.Radiobutton(mode_frame, text="Локальный датасет", variable=self.use_api, value=False,
                        command=self.update_mode).pack(side=tk.LEFT, padx=5)

        # Поле для ввода жанра
        ttk.Label(main_frame, text="Жанр:").grid(row=3, column=0, sticky=tk.W, pady=(0, 5))
        self.genre_var = tk.StringVar()
        genre_entry = ttk.Entry(main_frame, textvariable=self.genre_var, width=30)
        genre_entry.grid(row=3, column=1, sticky=(tk.W, tk.E), pady=(0, 5))

        # Подсказка по жанрам
        genres_text = ", ".join(list(self.agent.genres.values())[:10]) + "..."
        ttk.Label(main_frame, text=f"Доступные жанры: {genres_text}", font=("Arial", 8), foreground="gray").grid(row=4,
                                                                                                                 column=1,
                                                                                                                 sticky=tk.W,
                                                                                                                 pady=(
                                                                                                                     0,
                                                                                                                     10))

        # Поле для ввода года
        ttk.Label(main_frame, text="Год:").grid(row=5, column=0, sticky=tk.W, pady=(0, 5))
        self.year_var = tk.StringVar()
        year_entry = ttk.Entry(main_frame, textvariable=self.year_var, width=30)
        year_entry.grid(row=5, column=1, sticky=(tk.W, tk.E), pady=(0, 20))

        # Кнопка поиска
        search_button = ttk.Button(main_frame, text="Найти фильм", command=self.search_movie)
        search_button.grid(row=6, column=0, columnspan=2, pady=(0, 20))

        # Поле для вывода результата
        ttk.Label(main_frame, text="Результат:").grid(row=7, column=0, sticky=tk.W, pady=(0, 5))
        self.result_text = scrolledtext.ScrolledText(main_frame, width=80, height=20, wrap=tk.WORD)
        self.result_text.grid(row=8, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Статус бар
        self.status_var = tk.StringVar()
        self.status_var.set("Готов к работе")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.grid(row=1, column=0, sticky=(tk.W, tk.E))

        # Настройка расширения столбцов и строк
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        main_frame.rowconfigure(8, weight=1)

        # Обработка нажатия Enter в полях ввода
        genre_entry.bind('<Return>', lambda event: self.search_movie())
        year_entry.bind('<Return>', lambda event: self.search_movie())

    def update_mode(self):
        """Обновление режима работы"""
        self.agent = MovieAgent(use_api=self.use_api.get())
        self.status_var.set(f"Режим изменен на: {'API' if self.use_api.get() else 'Локальный датасет'}")

    def search_movie(self):
        # Получаем значения из полей ввода
        genre = self.genre_var.get().strip()
        year_str = self.year_var.get().strip()

        # Преобразуем год в число, если введен
        year = int(year_str) if year_str else None

        # Обновляем статус
        self.status_var.set("Поиск...")
        self.root.update()

        try:
            # Получаем рекомендацию
            recommendation = self.agent.recommend_movie(genre_name=genre, year=year)

            # Выводим результат
            self.result_text.delete(1.0, tk.END)

            if "error" in recommendation:
                self.result_text.insert(tk.END, recommendation["error"])
                self.result_text.insert(tk.END, "\n\nДоступные жанры в базе:\n")
                all_genres = list(self.agent.genres.values())
                self.result_text.insert(tk.END, ", ".join(all_genres))
            else:
                result = f"🎬 Как насчёт: {recommendation['title']} ({recommendation['year']})\n\n"
                result += f"📀 Жанр: {recommendation['genre']}\n\n"
                result += f"⭐ Рейтинг: {recommendation['rating']}/10\n\n"
                result += f"📖 Описание: {recommendation['description']}\n\n"
                result += f"ℹ️  Источник: {recommendation.get('source', 'неизвестно')}"

                self.result_text.insert(tk.END, result)

            self.status_var.set("Поиск завершен")

        except ValueError:
            self.result_text.delete(1.0, tk.END)
            self.result_text.insert(tk.END, "Ошибка: пожалуйста, введите корректный год.")
            self.status_var.set("Ошибка ввода")
        except Exception as e:
            self.result_text.delete(1.0, tk.END)
            self.result_text.insert(tk.END, f"Произошла ошибка: {e}")
            self.status_var.set("Ошибка")


def run_gui():
    root = tk.Tk()
    app = MovieBotGUI(root)
    root.mainloop()


if __name__ == "__main__":
    run_gui()