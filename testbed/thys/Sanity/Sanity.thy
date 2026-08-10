theory Sanity
  imports Main
begin

(* This file imports only Main so it runs under -l HOL with no AFP build.
   If you get zero states here, the problem is your LSP wiring, not the
   AFP session heaps. Expected: ~14 distinct states, including a visible
   goal count going 1 -> 2 -> 1 across the induction. *)

lemma rev_rev [simp]: "rev (rev xs) = xs"
  apply (induct xs)
   apply simp
  apply simp
  done

lemma add_zero: "x + 0 = (x::nat)"
proof -
  have "x + 0 = x" by simp
  thus ?thesis .
qed

lemma append_assoc_demo: "(xs @ ys) @ zs = xs @ (ys @ zs)"
  apply (induct xs)
   apply simp
  apply simp
  done

text ‹A markup block mentioning apply and qed, which must not be probed.›

lemma two_subgoals: "xs = [] \<or> length xs > 0"
  apply (cases xs)
   apply simp
  apply simp
  done

end
