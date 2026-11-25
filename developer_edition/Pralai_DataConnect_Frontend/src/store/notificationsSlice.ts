/*
 * Toast notifications — one of the "shared async state" domains called out
 * for Redux: every write across every page reports success/failure here.
 */
import { createSlice, type PayloadAction } from "@reduxjs/toolkit";
import type { AppDispatch } from "./store";

export interface Toast {
  id: number;
  msg: string;
  type: string;
}

let toastSeq = 0;
const TOAST_TTL_MS = 4200;

const notificationsSlice = createSlice({
  name: "notifications",
  initialState: [] as Toast[],
  reducers: {
    toastAdded(state, action: PayloadAction<Toast>) {
      state.push(action.payload);
    },
    toastRemoved(state, action: PayloadAction<number>) {
      return state.filter((t) => t.id !== action.payload);
    },
  },
});

export const { toastAdded, toastRemoved } = notificationsSlice.actions;

export const showToast = (msg: string, type = "") => (dispatch: AppDispatch) => {
  const id = ++toastSeq;
  dispatch(toastAdded({ id, msg, type }));
  setTimeout(() => dispatch(toastRemoved(id)), TOAST_TTL_MS);
};

export const selectToasts = (state: { notifications: Toast[] }) => state.notifications;
export default notificationsSlice.reducer;
