cd /home/skyggedans/Work/yt-trans
echo "=== кто на карте прямо сейчас ==="
nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader | while IFS=', ' read pid mem; do
  name=$(cat /proc/$pid/comm 2>/dev/null || echo '?')
  printf "  pid=%-8s %-10s %s\n" "$pid" "$mem" "$name"
done
echo
echo "=== где лежат веса распознавания ==="
du -sh ~/.cache/yt-trans/hf/hub/models--Systran--faster-whisper-large-v3 2>/dev/null \
  || du -sh ~/.cache/yt-trans/hf/hub/* 2>/dev/null | head -3
echo
echo "=== как это видит наш демон ==="
curl -s --max-time 5 http://127.0.0.1:8756/v1/health | python3 -c "
import json,sys; d=json.load(sys.stdin); g=d['gpu']
print(f\"  свободно {g['free_mib']} МиБ | занято {g['busy']} | чужие модели {g['foreign_models']}\")
print(f\"  распознавание: {d['models']['asr']['model']} (просит {d['models']['asr']['vram_mib']} МиБ)\")
print(f\"  перевод:       {d['models']['translator']['model']} (просит {d['models']['translator']['vram_mib']} МиБ)\")"
