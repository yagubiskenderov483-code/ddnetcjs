'use client';

import { motion } from 'framer-motion';
import { useEffect, useRef, useState } from 'react';
import { ExportProgress } from '@/components/ExportProgress';
import { GlassCard } from '@/components/GlassCard';
import { PairingPanel } from '@/components/PairingPanel';
import { StatusBadge } from '@/components/StatusBadge';
import { Toast, ToastKind } from '@/components/Toast';
import { api } from '@/lib/api';
import type { PairingCreated, PairingStatus } from '@/lib/types';

export default function Page() {
  const [pairing, setPairing] = useState<PairingCreated | null>(null);
  const [status, setStatus] = useState<PairingStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const [toast, setToast] = useState<{ open: boolean; kind: ToastKind; message: string }>({
    open: false, kind: 'info', message: '',
  });
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const showToast = (kind: ToastKind, message: string) => setToast({ open: true, kind, message });
  const closeToast = () => setToast((t) => ({ ...t, open: false }));

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  async function handleStart() {
    setStarting(true);
    try {
      const p = await api.createPairing();
      setPairing(p);
      setStatus({ state: 'awaiting_agent', progress: 0, stage: '', delivered: false, error: null, agent_info: null });
      showToast('info', 'Код сгенерирован. Запустите агент на своей машине.');

      pollRef.current = setInterval(async () => {
        try {
          const s = await api.status(p.polling_token);
          setStatus(s);
          if (s.state === 'done' || s.state === 'error' || s.state === 'expired') {
            if (pollRef.current) clearInterval(pollRef.current);
            pollRef.current = null;
            if (s.state === 'done') showToast('success', 'Архив доставлен в бот.');
            else if (s.state === 'expired') showToast('error', 'Код истёк. Начните заново.');
            else showToast('error', s.error ?? 'Ошибка');
          }
        } catch {
          // временные ошибки polling не роняют UI
        }
      }, 1500);
    } catch (e) {
      showToast('error', (e as Error).message);
    } finally {
      setStarting(false);
    }
  }

  function handleReset() {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    setPairing(null);
    setStatus(null);
  }

  const working = status && status.state !== 'done' && status.state !== 'error' && status.state !== 'expired';
  const badgeKind: 'idle' | 'connecting' | 'connected' | 'error' =
    !status ? 'idle'
    : status.state === 'error' || status.state === 'expired' ? 'error'
    : status.state === 'done' ? 'connected'
    : 'connecting';
  const badgeLabel =
    !status ? 'Не подключён'
    : status.state === 'awaiting_agent' ? 'Ожидание агента'
    : status.state === 'agent_connected' ? (status.agent_info ? `Агент: ${status.agent_info}` : 'Агент подключён')
    : status.state === 'exporting' ? 'Экспорт'
    : status.state === 'uploading' ? 'Загрузка'
    : status.state === 'forwarding' ? 'Отправка в бот'
    : status.state === 'done' ? 'Готово'
    : status.state === 'expired' ? 'Код истёк'
    : 'Ошибка';

  return (
    <main className="min-h-screen px-4 py-10 md:py-16 flex flex-col items-center">
      <motion.header
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5 }}
        className="w-full max-w-3xl mb-10 md:mb-14"
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="relative w-9 h-9 rounded-xl bg-gradient-to-br from-tg-blue to-cyan-400 grid place-items-center shadow-glow">
              <svg viewBox="0 0 24 24" className="w-5 h-5 text-black" fill="currentColor"><path d="M21.5 4.2 2.8 11.4c-1.3.5-1.3 1.7 0 2.1l4.7 1.5 1.8 5.8c.2.6.4.8.8.8.4 0 .6-.2 1-.5l2.2-2.1 4.6 3.4c.8.5 1.4.2 1.7-.8l3-14.1c.3-1.4-.5-2-1.4-1.4z"/></svg>
            </div>
            <div>
              <div className="text-xl font-semibold tracking-tight">Telegram Data Extractor</div>
              <div className="text-xs text-white/40">Полный экспорт в формате Telegram Desktop · локальный агент</div>
            </div>
          </div>
          <StatusBadge kind={badgeKind} label={badgeLabel} />
        </div>
      </motion.header>

      <div className="w-full max-w-xl space-y-6">
        {!pairing && !status && (
          <GlassCard>
            <h1 className="text-2xl font-semibold mb-1">Начать экспорт</h1>
            <p className="text-sm text-white/60 mb-6">
              Экспорт выполняется локальным агентом на вашем компьютере. Сервер получает только
              готовый ZIP, авторизация Telegram проходит на вашей машине.
            </p>
            <ul className="text-sm text-white/70 space-y-2 mb-6 list-disc pl-5 marker:text-tg-blue">
              <li>Полные сообщения, фото, видео, голосовые, документы</li>
              <li>Структура папок и HTML, как у Telegram Desktop Export</li>
              <li>Коды авторизации и 2FA на сервер не передаются</li>
              <li>Итоговый архив автоматически уйдёт в бота владельца</li>
            </ul>
            <button className="btn-primary w-full" onClick={handleStart} disabled={starting}>
              {starting ? 'Генерация кода…' : 'Сгенерировать pairing-код'}
            </button>
          </GlassCard>
        )}

        {pairing && status && status.state === 'awaiting_agent' && (
          <GlassCard>
            <h2 className="text-xl font-semibold mb-1">Запустите агент</h2>
            <PairingPanel code={pairing.pairing_code} expiresIn={pairing.expires_in} />
            <button className="btn-ghost w-full mt-6" onClick={handleReset}>
              Отменить
            </button>
          </GlassCard>
        )}

        {status && status.state !== 'awaiting_agent' && (
          <GlassCard>
            <h2 className="text-xl font-semibold mb-4">Прогресс</h2>
            <ExportProgress status={status} />
            {working && (
              <div className="mt-6 flex justify-center">
                <div className="relative w-16 h-16">
                  <div className="absolute inset-0 rounded-full border-2 border-tg-blue/30" />
                  <div className="absolute inset-0 rounded-full border-2 border-transparent border-t-tg-blue orbit" />
                </div>
              </div>
            )}
            {(status.state === 'done' || status.state === 'error' || status.state === 'expired') && (
              <button className="btn-ghost w-full mt-6" onClick={handleReset}>
                Закрыть
              </button>
            )}
          </GlassCard>
        )}

        <footer className="text-center text-xs text-white/30 pt-6">
          Открытый код · Экспорт выполняется локально · Сервер проверяет контрольную сумму перед доставкой
        </footer>
      </div>

      <Toast open={toast.open} kind={toast.kind} message={toast.message} onClose={closeToast} />
    </main>
  );
}
