import { Session } from '@supabase/supabase-js';

import { supabase } from './supabase';

export type PlanRow = {
  id: string;
  name: string;
  monthly_limit: number | null;
  daily_limit: number | null;
  price_usd: number | null;
  features: Record<string, any> | null;
};

export type SubscriptionRow = {
  id: string;
  user_id: string;
  plan_id: string;
  status: 'active' | 'past_due' | 'canceled';
  valid_until: string | null;
  started_at: string | null;
  created_at: string | null;
};

export type UsageSnapshot = {
  plan: PlanRow | null;
  subscription: SubscriptionRow | null;
  dailyUsed: number;
  monthlyUsed: number;
  remainingToday: number | null;
  remainingMonth: number | null;
};

export type ConsultaTipo =
  | 'sunarp'
  | 'soat'
  | 'licencia'
  | 'vehicular_full'
  | 'papeletas'
  | 'redam';

const serviceTipoMap: Record<string, ConsultaTipo> = {
  soat: 'soat',
  itv: 'vehicular_full',
  sunarp: 'sunarp',
  sutran: 'papeletas',
  satlima: 'papeletas',
  satcallao: 'papeletas',
  licencia: 'licencia',
  dniperu: 'vehicular_full',
  redam: 'redam',
};

export const mapServiceToConsultaTipo = (serviceKey: string): ConsultaTipo => {
  return serviceTipoMap[serviceKey] ?? 'vehicular_full';
};

export async function ensureUserBootstrap(session: Session | null) {
  if (!supabase || !session?.user) return;
  const { id: userId, user_metadata: meta, email } = session.user;

  try {
    const { data: profile, error: profileError } = await supabase
      .from('profiles')
      .select('user_id')
      .eq('user_id', userId)
      .maybeSingle();

    if (!profile && !profileError) {
      await supabase.from('profiles').insert({
        user_id: userId,
        full_name: meta?.full_name ?? meta?.name ?? email ?? 'Usuario',
        phone: meta?.phone_number ?? meta?.phone ?? null,
      });
    }

    const { data: subscription } = await supabase
      .from('subscriptions')
      .select('id, status')
      .eq('user_id', userId)
      .eq('status', 'active')
      .maybeSingle();

    if (!subscription) {
      await supabase.from('subscriptions').insert({
        user_id: userId,
        plan_id: 'free',
        status: 'active',
        valid_until: null,
      });
    }
  } catch (error) {
    console.warn('ensureUserBootstrap error', error);
  }
}

export async function getUsageSnapshot(userId?: string): Promise<UsageSnapshot> {
  if (!supabase || !userId) {
    return {
      plan: null,
      subscription: null,
      dailyUsed: 0,
      monthlyUsed: 0,
      remainingToday: null,
      remainingMonth: null,
    };
  }

  const dayStart = new Date();
  dayStart.setHours(0, 0, 0, 0);
  const monthStart = new Date();
  monthStart.setDate(1);
  monthStart.setHours(0, 0, 0, 0);

  const { data: subscription } = await supabase
    .from('subscriptions')
    .select('*')
    .eq('user_id', userId)
    .eq('status', 'active')
    .maybeSingle();

  let plan: PlanRow | null = null;
  if (subscription?.plan_id) {
    const { data } = await supabase
      .from('plans')
      .select('*')
      .eq('id', subscription.plan_id)
      .maybeSingle();
    plan = (data as PlanRow | null) ?? null;
  }

  const { count: dailyUsed = 0 } = await supabase
    .from('consultas')
    .select('id', { count: 'exact', head: true })
    .eq('user_id', userId)
    .gte('created_at', dayStart.toISOString());

  const { count: monthlyUsed = 0 } = await supabase
    .from('consultas')
    .select('id', { count: 'exact', head: true })
    .eq('user_id', userId)
    .gte('created_at', monthStart.toISOString());

  const remainingToday =
    plan?.daily_limit != null ? Math.max(0, plan.daily_limit - dailyUsed) : null;
  const remainingMonth =
    plan?.monthly_limit != null ? Math.max(0, plan.monthly_limit - monthlyUsed) : null;

  return {
    plan,
    subscription: (subscription as SubscriptionRow | null) ?? null,
    dailyUsed,
    monthlyUsed,
    remainingToday,
    remainingMonth,
  };
}

type RegisterConsultaInput = {
  userId: string;
  serviceKey: string;
  placa?: string | null;
  dni?: string | null;
  payload?: any;
  respuesta?: any;
  resumen?: string | null;
  success?: boolean;
  errorCode?: string | null;
  durationMs?: number | null;
  rawPath?: string | null;
};

export async function registerConsulta(input: RegisterConsultaInput) {
  if (!supabase || !input.userId) return;
  const tipo = mapServiceToConsultaTipo(input.serviceKey);
  const row = {
    user_id: input.userId,
    tipo,
    placa: input.placa ? input.placa.toUpperCase() : null,
    dni: input.dni ?? null,
    payload: input.payload ?? null,
    respuesta: input.respuesta ?? null,
    resumen: input.resumen ?? `${input.serviceKey} ${input.placa ?? input.dni ?? ''}`.trim(),
    success: input.success ?? true,
    error_code: input.errorCode ?? null,
    duracion_ms: input.durationMs ?? null,
    raw_path: input.rawPath ?? null,
  };

  try {
    await supabase.from('consultas').insert(row);
  } catch (error) {
    console.warn('registerConsulta error', error);
  }
}
