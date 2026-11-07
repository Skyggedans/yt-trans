cd /home/skyggedans/Work/yt-trans
echo "=== who is on the card right now ==="
nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader | while IFS=', ' read pid mem; do
  name=$(cat /proc/$pid/comm 2>/dev/null || echo '?')
  printf "  pid=%-8s %-10s %s\n" "$pid" "$mem" "$name"
done
echo
echo "=== where the recognition weights lie ==="
du -sh ~/.cache/yt-trans/hf/hub/models--Systran--faster-whisper-large-v3 2>/dev/null \
  || du -sh ~/.cache/yt-trans/hf/hub/* 2>/dev/null | head -3
echo
echo "=== how our daemon sees it ==="
curl -s --max-time 5 http://127.0.0.1:8756/v1/health | python3 -c "
import json,sys; d=json.load(sys.stdin); g=d['gpu']
print(f\"  free {g['free_mib']} MiB | busy {g['busy']} | foreign models {g['foreign_models']}\")
print(f\"  recognition: {d['models']['asr']['model']} (asks for {d['models']['asr']['vram_mib']} MiB)\")
print(f\"  translation:       {d['models']['translator']['model']} (asks for {d['models']['translator']['vram_mib']} MiB)\")"
