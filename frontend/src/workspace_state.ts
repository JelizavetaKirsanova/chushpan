export const phaseLabels: Record<string, string> = {
  idle: "Готов к следующему запросу", recognizing: "Распознаю речь", processing: "Обрабатываю запрос",
  switching: "Переключаю модель", generating: "Генерирую ответ", error: "Запрос не завершён",
  command: "Выполняю команду",
  listening: "Слушаю", waiting: "Короткая реплика ожидания", speaking: "Отвечаю",
  interrupted: "Прерван пользователем",
  wake_detection: "Проверяю обращение", follow_up_listening: "Слушаю продолжение",
  error_recovery: "Восстанавливаю прослушивание",
};

export const recipientLabels = {
  direct: "Обращение распознано", continuation: "Продолжение диалога", waiting: "Ожидание обращения",
  task: "Состояние предыдущей задачи", ambiguous: "Неоднозначное обращение — жду имя",
  task_running: "Выполняется предыдущая задача",
};

export function sessionCaption(session: {active: boolean; listening: boolean; requires_wake: boolean; recording?:boolean; paused?:boolean}): string {
  if (session.active && session.listening && session.recording === false) return "Ожидает продолжение без имени";
  if (session.active) return session.listening ? "Слушаю продолжение без имени" : "Диалоговая сессия активна";
  return session.requires_wake ? "Для обращения нужен wake-word: «Эй, Чушпан»" : "Свободное прослушивание включено";
}

export class RequestEvents {
  private latest = 0;
  accept(event: {request_id?: number}): boolean {
    if (event.request_id === undefined) return true;
    if (event.request_id < this.latest) return false;
    this.latest = event.request_id;
    return true;
  }
  reset(): void { this.latest = 0; }
}
export const healthLabels: Record<string, string> = {
  unknown: "Ещё не проверена", ready: "Доступна", open: "Временно исключена",
  probing: "Выполняется запрос", failed: "Ошибка запроса", cancelled: "Запрос отменён",
};
export function seconds(value: number | null | undefined): string {
  return value == null ? "—" : `${value.toFixed(2)} с`;
}
