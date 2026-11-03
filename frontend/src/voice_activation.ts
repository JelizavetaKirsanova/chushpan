/** Readiness of the microphone is independent of the conversation state. */
export function microphonePresentation(mode:string, microphone:string):{label:string;hint:string;button:string}|null {
  if(!['sleeping','listening'].includes(mode))return null;
  if(microphone==='stopped')return {label:'Микрофон выключен',hint:'Включи микрофон кнопкой ниже, чтобы Чушпан услышал обращение.',button:'Включить микрофон'};
  if(microphone==='starting')return {label:'Подготовка микрофона',hint:'Загружаю распознавание и голос. Пока микрофон не включён.',button:'Подготовка микрофона…'};
  return null;
}
export type MicrophoneApi={desktop_action:(action:string)=>Promise<{ok:boolean;message:string}>};
export async function requestMicrophone(api:MicrophoneApi|undefined, starting:()=>void, failed:(message:string)=>void):Promise<void> {
  if(!api?.desktop_action){failed('Включи прослушивание в меню установленного Чушпан.app.');return;}
  starting();
  try {
    const result=await api.desktop_action('listen');
    if(!result.ok)failed(result.message);
    // Only the backend's voice_settings event confirms that input actually started.
  } catch(error){failed('Не удалось включить микрофон: '+String(error));}
}

export function dialogueCaption(mode:string,session:{active:boolean;recording?:boolean}|null):string|null {
  if(mode!=='listening'||!session?.active)return null;
  return session.recording===false?'Ожидает продолжение':'Слушает';
}
