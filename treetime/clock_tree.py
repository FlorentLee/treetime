import utils
import numpy as np
import config as ttconf
from treeanc import TreeAnc
from distribution import Distribution
from branch_len_interpolator import BranchLenInterpolator
from node_interpolator import NodeInterpolator


class ClockTree(TreeAnc):
    """
    Class to produce molecular clock trees.
    """

    def __init__(self,  dates=None,*args, **kwargs):
        super(ClockTree, self).__init__(*args, **kwargs)
        if dates is None:
            raise("ClockTree requires date contraints!")
        self.date_dict = dates
        self.date2dist = None  # we do not know anything about the conversion
        self.max_diam = 0.0
        self.debug=False

        for node in self.tree.find_clades():
            if node.name in self.date_dict:
                node.numdate_given = self.date_dict[node.name]
            else:
                node.numdate_given = None


    @property
    def date2dist(self):
        return self._date2dist

    @date2dist.setter
    def date2dist(self, val):
        if val is None:
            self._date2dist = None
            return
        else:
            self.logger("TreeTime.date2dist: Setting new date to branchlength conversion. slope=%f, R2=%.4f"%(val.slope, val.r_val), 2)
            self._date2dist = val

    def init_date_constraints(self, ancestral_inference=True, slope=None, **kwarks):
        """
        Get the conversion coefficients between the dates and the branch
        lengths as they are used in ML computations. The conversion formula is
        assumed to be 'length = k*numdate_given + b'. For convenience, these
        coefficients as well as regression parameters are stored in the
        dates2dist object.

        Note: that tree must have dates set to all nodes before calling this
        function. (This is accomplished by calling load_dates func).
        """
        self.logger("ClockTree.init_date_constraints...",2)

        if ancestral_inference or (not hasattr(self.tree.root, 'sequence')):
            self.optimize_seq_and_branch_len(**kwarks)

        # set the None  for the date-related attributes in the internal nodes.
        # make interpolation objects for the branches
        print('\n----- Initializing branch length interpolation objects...\n')
        for node in self.tree.find_clades():
            if node.up is not None:
                node.branch_length_interpolator = BranchLenInterpolator(node, self.gtr, one_mutation=self.one_mutation)
            else:
                node.branch_length_interpolator = None
        self.date2dist = utils.DateConversion.from_tree(self.tree, slope)
        self.max_diam = self.date2dist.intercept

        # make node distribution objects
        for node in self.tree.find_clades():
            # node is constrained
            if hasattr(node, 'numdate_given') and node.numdate_given is not None:
                if hasattr(node, 'bad_branch') and node.bad_branch==True:
                    print ("Branch is marked as bad, excluding it from the optimization process"
                        " Will be optimized freely")
                    node.numdate_given = None
                    node.abs_t = None
                    # if there are no constraints - log_prob will be set on-the-fly
                    node.msg_to_parent = None
                else:
                    # set the absolute time before present in branch length units
                    node.abs_t = (utils.numeric_date() - node.numdate_given) * abs(self.date2dist.slope)
                    node.msg_to_parent = NodeInterpolator.delta_function(node.abs_t, weight=1)

            else: # node without sampling date set
                node.numdate_given = None
                node.abs_t = None
                # if there are no constraints - log_prob will be set on-the-fly
                node.msg_to_parent = None

    def make_time_tree(self):
        '''
        use the date constraints to calculate the most likely positions of
        unconstraint nodes.
        '''
        myTree._ml_t_leaves_root()
        myTree._ml_t_root_leaves()
        myTree._set_final_dates()
        myTree.convert_dates()


    def _ml_t_leaves_root(self):
        """
        Compute the probability distribution of the internal nodes positions by
        propagating from the tree leaves towards the root. The result of
        this operation are the probability distributions of each internal node,
        conditional on the constraints on leaves in the descendant subtree. The exception
        is the root of the tree, as its subtree includes all the constrained leaves.
        To the final location probability distribution of the internal nodes,
        is calculated via back-propagation in _ml_t_root_to_leaves.

        Args:

         - None: all required parameters are pre-set as the node attributes during
           tree preparation

        Returns:

         - None: Every internal node is assigned the probability distribution in form
           of an interpolation object and sends this distribution further towards the
           root.

        """
        def _send_message(node, **kwargs):
            """
            Calc the desired LH distribution of the parent
            """
            if node.msg_to_parent.is_delta:
                res = Distribution.shifted_x(node.branch_length_interpolator, node.msg_to_parent.peak_pos)
            else: # convolve two distributions
                res =  NodeInterpolator.convolve(node.msg_to_parent, node.branch_length_interpolator)
                # TODO deal with grid size explosion
            return res

        self.logger("ClockTree: Maximum likelihood tree optimization with temporal constraints:",1)
        self.logger("ClockTree: Propagating leaves -> root...", 2)
        # go through the nodes from leaves towards the root:
        for node in self.tree.find_clades(order='postorder'):  # children first, msg to parents
            if node.is_terminal():
                node.msgs_from_leaves = {}
            else:
                # save all messages from the children nodes with constraints
                # store as dictionary to exclude nodes from the set when necessary
                # (see below)
                node.msgs_from_leaves = {clade: _send_message(clade) for clade in node.clades
                                                if clade.msg_to_parent is not None}

                if len(node.msgs_from_leaves) < 1:  # we need at least one constraint
                    continue
                # this is what the node sends to the parent
                node.msg_to_parent = NodeInterpolator.multiply(node.msgs_from_leaves.values())


    def _ml_t_root_leaves(self):
        """
        Given the location probability distribution, computed by the propagation
        from leaves to root, set the root most-likely location. Estimate the
        tree likelihood. Report the root location probability distribution
        message towards the leaves. For each internal node, compute the final
        location probability distribution based on the pair of messages (from the
        leaves and from the root), and find the most likely position of the
        internal nodes and finally, convert it to the date-time information

        Args:

        - None: all the requires parameters are pre-set in the previous steps.

        Returns:
         - None: all the internal nodes are assigned probability distributions
           of their locations. The branch lengths are updated to reflect the most
           likely node locations.

        """
        self.logger("ClockTree: Propagating root -> leaves...", 2)
        # Main method - propagate from root to the leaves and set the LH distributions
        # to each node
        for node in self.tree.find_clades(order='preorder'):  # ancestors first, msg to children
            ## This is the root node
            if node.up is None:
                node.msg_from_parent = None # nothing beyond the root
            else:
                parent = node.up
                complementary_msgs = [parent.msgs_from_leaves[k]
                                      for k in parent.msgs_from_leaves
                                      if k != node]

                if parent.msg_from_parent is not None: # the parent is not root => got something from the parent
                    complementary_msgs.append(parent.msg_from_parent)

                msg_parent_to_node = NodeInterpolator.multiply(complementary_msgs)
                res = NodeInterpolator.convolve(msg_parent_to_node, node.branch_length_interpolator,
                                                inverse_time=False)
                node.msg_from_parent = res


    def _set_final_dates(self):
        """
        Given the location of the node in branch length units, convert it to the
        date-time information.

        Args:
         - node(Phylo.Clade): tree node. NOTE the node should have the abs_t attribute
         to have a valid value. This is automatically taken care of in the
         procedure to get the node location probability distribution.

        """
        self.logger("ClockTree: Setting dates and node distributions...", 2)
        def collapse_func(dist):
            if dist.is_delta:
                return dist.peak_pos
            else:
                return dist.peak_pos


        for node in self.tree.find_clades(order='preorder'):  # ancestors first, msg to children
            # set marginal distribution
            ## This is the root node
            if node.up is None:
                node.marginal_lh = node.msg_to_parent
            else:
                node.marginal_lh = NodeInterpolator.multiply((node.msg_from_parent, node.msg_to_parent))

            if node.up is None:
                node.joint_lh = node.msg_to_parent
                node.time_before_present = collapse_func(node.joint_lh)
                node.branch_length = self.one_mutation
            else:
                # shift position of parent node (time_before_present) by the branch length
                # towards the present. To do so, add branch length to negative time_before_present
                # and rescale the resulting distribution by -1.0
                res = Distribution.shifted_x(node.branch_length_interpolator, -node.up.time_before_present)
                res.x_rescale(-1.0)
                # multiply distribution from parent with those from children and determine peak
                if node.msg_to_parent is not None:
                    node.joint_lh = NodeInterpolator.multiply((node.msg_to_parent, res))
                else:
                    node.joint_lh = res
                node.time_before_present = collapse_func(node.joint_lh)

                node.branch_length = node.up.time_before_present - node.time_before_present
            node.clock_length = node.branch_length



    def convert_dates(self):
        from datetime import datetime, timedelta
        now = utils.numeric_date()
        for node in self.tree.find_clades():
            years_bp = self.date2dist.get_date(node.time_before_present)
            if years_bp < 0:
                if not hasattr(node, "bad_branch") or node.bad_branch==False:
                    self.logger("ClockTree.convert_dates: ERROR: The node is later than today, but it is not"
                        "marked as \"BAD\", which indicates the error in the "
                        "likelihood optimization.",4 , warn=True)
                else:
                    self.logger("ClockTree.convert_dates: Warning! node, which is marked as \"BAD\" optimized "
                        "later than present day",4 , warn=True)

            node.numdate = now - years_bp

            # set the human-readable date
            days = 365.25 * (node.numdate - int(node.numdate))
            year = int(node.numdate)
            try:
                n_date = datetime(year, 1, 1) + timedelta(days=days)
                node.date = datetime.strftime(n_date, "%Y-%m-%d")
            except:
                # this is the approximation
                n_date = datetime(1900, 1, 1) + timedelta(days=days)
                node.date = str(year) + "-" + str(n_date.month) + "-" + str(n_date.day)


if __name__=="__main__":
    import matplotlib.pyplot as plt
    plt.ion()

    with open('data/H3N2_NA_allyears_NA.20.metadata.csv') as date_file:
        dates = {}
        for line in date_file:
            try:
                name, date = line.strip().split(',')
                dates[name] = float(date)
            except:
                continue

    from Bio import Phylo
    tree = Phylo.read("data/H3N2_NA_allyears_NA.20.nwk", 'newick')
    tree.root_with_outgroup([n for n in tree.get_terminals()
                              if n.name=='A/New_York/182/2000|CY001279|02/18/2000|USA|99_00|H3N2/1-1409'][0])
    myTree = ClockTree(gtr='Jukes-Cantor', tree = tree,
                        aln = 'data/H3N2_NA_allyears_NA.20.fasta', verbose = 6, dates = dates)

    myTree.optimize_seq_and_branch_len(prune_short=True)
    myTree.init_date_constraints()
    myTree.make_time_tree()

    plt.figure()
    x = np.linspace(0,0.05,100)
    for node in myTree.tree.find_clades():
        if node.up is not None:
            print(node.branch_length_interpolator.peak_val, node.mutations)
            plt.plot(x, node.branch_length_interpolator.prob(x))
    plt.yscale('log')

    plt.figure()
    x = np.linspace(0,0.2,1000)
    for node in myTree.tree.find_clades():
        if (not node.is_terminal()):
            #print(node.branch_length_interpolator.peak_val)
            print(node.date)
            plt.plot(x, node.marginal_lh.prob_relative(x), '-')
            plt.plot(x, node.joint_lh.prob_relative(x), '--')
#    plt.yscale('log')

