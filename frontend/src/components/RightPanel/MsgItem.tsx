import type { Message } from '../../types/session';
import Markdown from '../Markdown';

export default function MsgItem({ msg }: { msg: Message }) {
  const isTutor = msg.role === 'tutor';

  return (
    <div className={`flex ${isTutor ? 'justify-start' : 'justify-end'}`}>
      <div
        className={`max-w-[85%] rounded-lg px-3 py-2 text-sm leading-relaxed ${
          isTutor
            ? 'bg-ct-panel text-ct-text'
            : 'bg-ct-accent/20 text-ct-text'
        }`}
      >
        <Markdown content={msg.content} />
      </div>
    </div>
  );
}