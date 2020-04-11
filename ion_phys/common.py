import numpy as np
from collections import namedtuple
import scipy.constants as consts

from ion_phys import operators
from ion_phys.utils import Lande_g

_uB = consts.physical_constants["Bohr magneton"][0]
_uN = consts.physical_constants["nuclear magneton"][0]

# frequencies in angular units
Level = namedtuple("Level", "n,L,J,S")
Transition = namedtuple("Transition", "lower,upper,freq,A")
Laser = namedtuple("Laser", "transition,q,I,delta")


class LevelData:
    """ Stored atomic structure information about a single level. """
    def __init__(self, g_J=None, g_I=None, Ahfs=None, Bhfs=None):
        """
        :param g_J: G factor. If None, we use the Lande g factor.
        :param g_I: Nuclear g factor.
        :param Ahfs: Nuclear A coefficient
        :param Bhfs: Nuclear B quadrupole coefficient
        """
        self.g_J = g_J
        self.g_I = g_I
        self.Ahfs = Ahfs
        self.Bhfs = Bhfs

        self.E = None  # In angular frequency units
        self._num_states = None
        self._start_ind = None
        self._stop_ind = None

    def slice(self):
        """ Returns a slice object that selects the states within a given
        level.

        Internally, we store states in order of increasing energy. This
        provides a more convenient means of accessing the states within a
        given level.
        """
        return slice(self._start_ind, self._stop_ind)

    def __repr__(self):
        return ("LevelData(g_J={}, g_I={}, E={}, num_states={}, start_ind={}, "
                " stop_ind={})""".format(self.g_J, self.g_I, self.E,
                                         self._num_states, self._start_ind,
                                         self._stop_ind))


class Ion:
    """ Base class for storing atomic structure data. """

    def __init__(self, B=None, *, I=0, levels={}, transitions={},
                 level_filter=None, transition_filter=None):
        """
        :param B: Magnetic field (T). To change the B-field later, call
          :meth setB:
        :param I: Nuclear spin
        :param levels: dictionary mapping Level:LevelData
        :param transitions: dictionary mapping transition name strings to
          Transition objects.
        :param level_filter: list of Levels to include in the simulation, if
            None we include all levels.
        :param transition_filter: list of Transitions to include in the
          simulation, if None we include all relevant transitions.

        Internally, we store all information as vectors/matrices with states
        ordered in terms of increasing energies.
        """
        self.B = None
        self.I = I

        levels = dict(levels)
        transitions = dict(transitions)

        if level_filter is not None:
            levels = dict(filter(lambda lev: lev[0] in level_filter,
                                 levels.items()))
        if transition_filter is None:
            transition_filter = transitions.keys()

        transition_filter = [transition for transition in transition_filter if
                             transition.lower in levels.keys()
                             and transition.upper in levels.keys()]

        transitions = dict(filter(lambda trans: trans[0] in transition_filter,
                                  transitions.items()))

        self.levels = levels
        self.transitions = transitions

        # ordered in terms of increasing state energies
        self.num_states = None  # Total number of electronic states
        self.M = None  # Magnetic quantum number of each state
        self.F = None  # F for each state (valid at low field)
        self.MI = None  # MI for each state (only valid at low field)
        self.MJ = None  # MJ for each state (only valid at low field)
        self.E = None  # State energies in angular frequency units

        self.E1 = None  # Electric dipole matrix elements
        self.E2 = None  # Electric quadrupole matrix elements
        self.M1 = None  # Magnetic dipole matrix elements
        self.Gamma = None  # scattering rates

        # V - V[:, i] is the state with energy E[i], represented in the basis
        #    high-field (MI, MJ) energy eigenstates.
        #  MI, MJ - high-field energy eigenstate basis vectors
        # V[:, i] are the expansion coefficients for the state E[i] in the
        # basis of high-field (MI, MJ) energy eigenstates.
        # NB the MI, MJ axes here are the most convenient to represent our
        # operators in; they are not energy ordered (like self.MI, self.MJ)
        self.V = None
        self.MIax = None
        self.MJax = None

        for level, data in self.levels.items():
            if data.g_J is None:
                data.g_J = Lande_g(level)

        self._sort_levels()  # arrange levels in energy order

        if B is not None:
            self.setB(B)

    def slice(self, level):
        """ Returns a slice object that selects the states within a given
        level.

        Internally, we store states in order of increasing energy. This
        provides a more convenient means of accessing the states within a
        given level.
        """
        return self.levels[level].slice()

    def index(self, level, M, *, F=None, MI=None, MJ=None):
        """ Returns the index of a state.

        If no kwargs are given, we return an array of indices of all states
        with a given M. The kwargs can be used to filter the results, for
        example, only returning the state with a given F.

        Valid kwargs: F, MI, MJ

        Internally, we store states in order of increasing energy. This
        provides a more convenient means of accessing a state.
        """
        lev = self.slice(level)
        Mvec = self.M[lev]
        inds = Mvec == M

        if F is not None:
            Fvec = self.F[lev]
            inds = np.logical_and(inds, Fvec == F)
        if MI is not None:
            MIvec = self.MI[lev]
            inds = np.logical_and(inds, MIvec == MI)
        if MJ is not None:
            MJvec = self.MJ[lev]
            inds = np.logical_and(inds, MJvec == MJ)

        inds = np.argwhere(inds)
        if len(inds) == 1:
            inds = int(inds)
        return inds

    def level(self, state):
        """ Returns the level a state lies in. """
        for level, data in self.levels.items():
            if state in data.slice():
                return level
        raise ValueError("No state with index {}".format(state))

    def delta(self, lower, upper):
        """ Returns the detuning of the transition between a pair of states
        from the overall centre of gravity of the set of transitions between
        the levels containing those states.

        If both states are in the same level, this returns the transition
        frequency.

        :param lower: index of the lower state
        :param upper: index of the upper state
        :return: the detuning (rad/s)
        """
        return self.E[upper] - self.E[lower]

    def _sort_levels(self):
        """ Use the transition data to sort the atomic levels in order of
        increasing energy.
        """
        if not(self.transitions):
            levels = list(self.levels.keys())
            if len(levels) != 1:
                raise ValueError("Disconnected level structure.")
            sorted_levels = {levels[0]: 0}
            unsorted = []
        else:
            unsorted = list(self.transitions.keys())
            lower, upper, dE, _ = self.transitions[unsorted.pop()]
            sorted_levels = {lower: 0, upper: dE}

        while unsorted:
            for trans in unsorted:
                lower, upper, dE, _ = self.transitions[trans]
                if lower in sorted_levels:
                    sorted_levels[upper] = sorted_levels[lower]+dE
                    break
                elif upper in sorted_levels:
                    sorted_levels[lower] = sorted_levels[upper]-dE
                    break
            else:
                raise ValueError(
                    "Transition '{}' would lead to a disconnected level"
                    " structure.".format(trans))
            unsorted.remove(trans)

        if sorted_levels.keys() != self.levels.keys():
            raise ValueError("Disconnected level structure")

        sorted_levels = sorted(sorted_levels.items(), key=lambda x: x[1])
        E0 = sorted_levels[0][1]  # ground-state energy
        start_ind = 0
        for level, energy in sorted_levels:
            data = self.levels[level]
            data.E = energy - E0
            data._num_states = int(np.rint((2*self.I + 1)*(2*level.J + 1)))
            data._start_ind = start_ind
            start_ind = data._stop_ind = start_ind + data._num_states

        self.num_states = start_ind

    def setB(self, B):
        """ Calculate atomic data at a given B-field (Tesla). """
        self.B = B
        self.M = np.zeros(self.num_states)
        self.F = np.zeros(self.num_states)
        self.E = np.zeros(self.num_states)
        self.MI = np.zeros(self.num_states)
        self.MJ = np.zeros(self.num_states)
        self.MIax = np.zeros(self.num_states)
        self.MJax = np.zeros(self.num_states)
        self.V = np.zeros((self.num_states, self.num_states))

        I = self.I
        I_dim = np.rint(2.0*I+1).astype(int)

        for level, data in self.levels.items():

            J = level.J
            J_dim = np.rint(2.0*J+1).astype(int)

            Jp = np.kron(operators.Jp(J), np.identity(I_dim))
            Jm = np.kron(operators.Jm(J), np.identity(I_dim))
            Jz = np.kron(operators.Jz(J), np.identity(I_dim))

            Ip = np.kron(np.identity(J_dim), operators.Jp(I))
            Im = np.kron(np.identity(J_dim), operators.Jm(I))
            Iz = np.kron(np.identity(J_dim), operators.Jz(I))

            H = data.g_J*_uB*B*Jz
            if self.I != 0:
                gI = data.g_I
                IdotJ = (Iz@Jz + (1/2)*(Ip@Jm + Im@Jp))

                H += - gI*_uN*B*Iz
                H += data.Ahfs*IdotJ

                if J > 1/2:
                    IdotJ2 = np.linalg.matrix_power(IdotJ, 2)
                    ident = np.identity(I_dim*J_dim)
                    H += data.Bhfs/(2*I*J*(2*I-1)*(2*J-1))*(
                        3*IdotJ2 + (3/2)*IdotJ - ident*I*(I+1)*J*(J+1))

            H /= consts.hbar  # work in angular frequency units
            lev = data.slice()
            E, V = np.linalg.eig(H)
            inds = np.argsort(E)

            self.E[lev] = E[inds]
            self.V[lev, lev] = V = V[:, inds]
            self.MIax[lev] = np.kron(np.ones(J_dim), np.arange(-I, I + 1))
            self.MJax[lev] = np.kron(np.arange(-J, J + 1), np.ones(I_dim))
            self.MI[lev] = np.rint(2*np.diag(V.conj().T@(Iz)@V))/2
            self.MJ[lev] = np.rint(2*np.diag(V.conj().T@(Jz)@V))/2
            self.M[lev] = M = np.rint(2*np.diag(V.conj().T@(Iz+Jz)@V))/2

            F_list = np.arange(I-J, I+J+1)
            if data.Ahfs < 0:
                F_list = F_list[::-1]

            for _M in set(M):
                for Fidx, idx in np.ndenumerate(np.where(M == _M)):
                    self.F[lev][idx] = F_list[_M <= F_list][Fidx[1]]

        if self.E1 is not None:
            self.calc_E1()
        if self.E2 is not None:
            self.calc_E2()
        if self.M1 is not None:
            self.calc_M1()
        if self.Gamma is not None:
            self.calc_Scattering()

    def calc_E1(self):
        """ Calculate the electric dipole matrix elements """
        pass
    #     self.Gamma = np.zeros((self.num_states, self.num_states))
    #     for _, trans in self.transitions:
    #         A = trans.A
    #         J = trans.upper.J  # check upper/lower!
    #         Mu = self.M[trans.upper.slice()]
    #         Ml = self.M[trans.lower.slice()]
    #         Gamma = self.Gamma(trans.upper.slice(), trans.lower.slice())
    #         # store GammaJ?
    #         for M in Mu:
    #             for q in [-1., 0., 1.]:
    #                 if M-q not in Ml:
    #                     continue
    #             Gamma[Mu==M, Ml==(M+q)] = A*(2*J+1)*3J(...)**2

    def calc_E2(self):
        """ Calculate electric quadrupole matrix elements. """
        pass

    def calc_Scattering(self):
        """ Calculate scattering rates from each state. """
        if self.E1 is None:
            self.calc_E1()

    def calc_M1(self):
        """ Calculates the matrix elements for M1 transitions within each
        level.

        The matrix elements, Rij, are defined so that:
          - R[i, j] := (-1)**(q+1)<i|u_q|j>
          - q := Mi - Mj = (-1, 0, 1)
          - u_q is the qth component of the magnetic dipole operator in
            spherical coordinates.

        NB with this definition, the Rabi frequency is given by:
          - hbar * W = B_-q * R
          - t_pi = pi/W
          - where B_-q is the -qth component of the magnetic field in spherical
            coordinates.
        """
        self.M1 = np.zeros((self.num_states, self.num_states))
        I = self.I
        I_dim = np.rint(2.0*I+1).astype(int)
        eyeI = np.identity(I_dim)

        for level, data in self.levels.items():
            lev = level.slice()
            J_dim = np.rint(2.0*level.J+1).astype(int)
            dim = J_dim*I_dim
            eyeJ = np.identity(J_dim)

            # magnetic dipole operator in spherical coordinates
            Jp = np.kron((-1/np.sqrt(2))*operators.Jp(level.J), eyeI)
            Jm = np.kron((+1/np.sqrt(2))*operators.Jm(level.J), eyeI)
            Jz = np.kron(operators.Jz(level.J), eyeI)

            Ip = np.kron(eyeJ, (-1/np.sqrt(2))*operators.Jp(I))
            Im = np.kron(eyeJ, (+1/np.sqrt(2))*operators.Jm(I))
            Iz = np.kron(eyeJ, operators.Jz(I))

            up = (-data.g_J*_uB*Jp + data.g_I*_uN*Ip)
            um = (-data.g_J*_uB*Jm + data.g_I*_uN*Im)
            uz = (-data.g_J*_uB*Jz + data.g_I*_uN*Iz)

            u = [um, uz, up]

            Mj = np.tile(data.M[lev], (dim, 1))
            Mi = Mj.T
            Q = (Mi - Mj)

            valid = (np.abs(Q) <= 1)
            valid[np.diag_indices(dim)] = False

            M1 = np.zeros((dim, dim))
            V = self.V[lev, lev]
            for transition in np.nditer(np.nonzero(valid)):
                i = transition[0]
                j = transition[1]
                q = np.rint(Q[i, j]).astype(int)

                psi_i = V[:, i]
                psi_j = V[:, j]

                M1[i, j] = ((-1)**(q+1)) * psi_i.conj().T@u[q+1]@psi_j
            self.M1[lev, lev] = M1
