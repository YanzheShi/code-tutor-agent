export default function LoadingScreen({
  progressMsgs, errorMsg, onRetry,
}: {
  progressMsgs: string[];
  errorMsg?: string;
  onRetry: () => void;
}) {
  if (errorMsg) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="text-center">
          <p className="text-lg text-ct-error">出错了</p>
          <p className="mt-1 text-sm text-ct-muted">{errorMsg}</p>
          <button onClick={onRetry} className="mt-4 rounded bg-ct-accent px-4 py-2 text-sm text-white">重试</button>
        </div>
      </div>
    );
  }
  return (
    <div className="flex h-screen flex-col items-center justify-center gap-4">
      <div className="flex items-center gap-2">
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" />
        <p className="text-lg text-ct-muted">出题中，请稍候...</p>
      </div>
      {progressMsgs.length > 0 && (
        <div className="max-w-md space-y-1">
          {progressMsgs.map((msg, i) => (
            <p key={i} className={'text-sm ' + (i === progressMsgs.length - 1 ? 'text-ct-accent' : 'text-ct-muted/60')}>{msg}</p>
          ))}
        </div>
      )}
    </div>
  );
}