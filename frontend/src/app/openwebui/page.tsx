import { redirect } from 'next/navigation';

export default function OpenWebUIPage() {
  redirect('/connections?tab=openwebui');
}
