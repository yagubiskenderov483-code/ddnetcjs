'use client';

import { motion } from 'framer-motion';
import { useEffect, useState } from 'react';

export function PairingPanel({ code, expiresIn }: { code: string; expiresIn: number }) {
  const [left, setLeft] = useState(expiresIn);

  useEffect(() => {
    const t = setInterval(() => setLeft((v) => Math.max(0, v - 1)), 1000);
    return () => clearInterval(t);
  }, []);

  const mm = Math.floor(left / 60).toString().padStart(2, '0');
  const ss = (left % 60).toString().padStart(2, '0');
  const letters = code.split('');

  return (
    <div className="space-y-5">
      <div className="text-sm text-white/60">
        На своей машине установите агент и запустите команду ниже. Код действителен ещё{' '}
        <span className="font-mono text-white/90">{mm}:{ss}</span>.
      </div>

      <motion.div
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        className="flex items-center justify-center gap-2 py-6"
      >
        {letters.map((c, i) => (
          <motion.span
            key={i}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.08 * i }}
            className="w-11 h-14 md:w-14 md:h-16 grid place-items-center rounded-xl bg-white/5 border border-white/10 font-mono text-2xl md:text-3xl font-semibold text-tg-blue"
          >
            {c}
          </motion.span>
        ))}
      </motion.div>

      <div className="rounded-xl bg-black/30 border border-white/5 p-4 font-mono text-xs text-white/80 overflow-x-auto">
        <div className="text-white/40 mb-2"># Установка агента (один раз)</div>
        pip install tde-agent
        <div className="text-white/40 mt-3 mb-2"># Запуск экспорта</div>
        export TG_API_ID=1234567<br />
        export TG_API_HASH=abcdef...<br />
        tde-agent pair <span className="text-tg-blue">{code}</span>
      </div>

      <p className="text-xs text-white/40">
        Коды авторизации Telegram и пароль 2FA агент запрашивает локально на вашей машине.
        На сервер они не попадают. Сервер получает только готовый ZIP-архив после подтверждения
        контрольной суммы.
      </p>
    </div>
  );
}
